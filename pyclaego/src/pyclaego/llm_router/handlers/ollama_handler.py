"""Ollama-protocol handlers — versatile dual-upstream dispatch.

Inbound requests arrive in Ollama wire format (/api/chat, /api/generate, …).
The handler resolves the `model` alias against *both* protocol namespaces:

1. ``protocol: ollama`` upstream found  → pure pass-through (NDJSON ↔ NDJSON).
2. ``protocol: openai`` upstream found  → translate Ollama↔OpenAI on the fly.

This means Ollama clients can transparently reach any OpenAI-compatible
upstream (OpenRouter, Moonshot, DeepSeek, …) configured in llm_router.yaml,
as well as real Ollama daemons, using the same /api/chat endpoint.

Resolution order: ollama first, then openai. If neither has the alias → 404.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import usage_extract
from ..forwarder import filter_response_headers
from ..recording.stream_merger import merge_ollama_stream, merge_openai_stream
from ..routing import ResolvedRoute
from ..translators import ollama_openai as _tr
from .base import HandlerContext, _rlog, merge_outbound_headers, now, record_call


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


async def _read_json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


def _resolve_multi(ctx: HandlerContext, alias: str) -> ResolvedRoute:
    """Resolve alias against 'ollama' then 'openai'; 404 if neither matches."""
    if not isinstance(alias, str) or not alias:
        raise HTTPException(status_code=400, detail="missing 'model' in request body")
    route = ctx.routes.resolve("ollama", alias)
    if route is None:
        route = ctx.routes.resolve("openai", alias)
    if route is None:
        _rlog.warning(
            "llm_router",
            f"[ollama] 404 no route for alias '{alias}' under ollama or openai",
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "model_not_found",
                    "message": f"no route for model '{alias}' under ollama or openai protocol",
                }
            },
        )
    return route


def _resolve_from_body(ctx: HandlerContext, body: dict[str, Any]) -> ResolvedRoute:
    alias = body.get("model")
    return _resolve_multi(ctx, alias)


# ─────────────────────────── Path helpers ───────────────────────────────────

def _ollama_chat_path(inbound_path: str) -> str:
    """Map an Ollama inbound path to the path used when forwarding to Ollama upstream."""
    return inbound_path  # /api/chat → /api/chat, /api/generate → /api/generate


def _openai_path_for(inbound_path: str) -> str:
    """Map an Ollama inbound path to the OpenAI upstream path."""
    if "generate" in inbound_path:
        # /api/generate → single-turn chat
        return "/chat/completions"
    if "embed" in inbound_path:
        return "/embeddings"
    return "/chat/completions"  # /api/chat and anything else




class OllamaUnaryHandler:
    @staticmethod
    async def handle(request: Request, path: str) -> Response:
        ctx = HandlerContext(request)
        body = await _read_json_body(request)
        route = _resolve_from_body(ctx, body)

        upstream_protocol = route.upstream.protocol

        if upstream_protocol == "openai":
            # ── Translate Ollama → OpenAI ─────────────────────────────────
            if "generate" in path:
                forward_body = _tr.req_ollama_generate_to_openai(body)
            else:
                forward_body = _tr.req_ollama_chat_to_openai(body)
            forward_body["model"] = route.upstream_model
            forward_body["stream"] = False
            upstream_path = _openai_path_for(path)
        else:
            # ── Native Ollama pass-through ────────────────────────────────
            forward_body = dict(body)
            forward_body["model"] = route.upstream_model
            forward_body["stream"] = False
            upstream_path = _ollama_chat_path(path)

        content = json.dumps(forward_body).encode("utf-8")
        headers = merge_outbound_headers(
            request.headers,
            route.upstream.headers,
            _auth_headers(route.upstream.api_key),
        )
        headers["content-type"] = "application/json"

        started = now()
        status: int | None = None
        resp_headers: dict[str, str] | None = None
        resp_body: Any = None
        error: str | None = None
        try:
            resp = await ctx.forwarder.forward_unary(
                upstream=route.upstream,
                method="POST",
                path=upstream_path,
                headers=headers,
                content=content,
            )
            first_byte = now()
            status = resp.status_code
            resp_headers = dict(resp.headers)
            try:
                resp_body = resp.json()
            except Exception:
                resp_body = resp.text

            if upstream_protocol == "openai":
                usage = usage_extract.extract_openai_unary(resp_body)
                if status is not None and status < 400 and isinstance(resp_body, dict):
                    resp_body = _tr.resp_openai_to_ollama(resp_body, route.alias)
            else:
                usage = usage_extract.extract_ollama_unary(resp_body)

            if status >= 400:
                _rlog.warning("llm_router", f"[ollama] {route.alias} upstream {status}: {resp_body}")
        except Exception as exc:
            error = type(exc).__name__
            _rlog.exception("llm_router", f"[ollama] unary forward failed for {route.alias}: {exc}")
            usage = (None, None, None)
            first_byte = None
            resp_body = {"error": str(exc)}
            status = 502
            resp_headers = {"content-type": "application/json"}
        finished = now()

        await record_call(
            ctx=ctx,
            route=route,
            method="POST",
            url=str(route.upstream.base_url) + upstream_path,
            request_headers=headers,
            request_params=None,
            request_body=forward_body,
            response_status=status,
            response_headers=resp_headers,
            response_body=resp_body,
            started_at=started,
            first_byte_at=first_byte,
            finished_at=finished,
            stream=False,
            usage=usage,
            error=error,
        )

        out_headers = filter_response_headers(resp_headers or {})
        if isinstance(resp_body, (dict, list)):
            return JSONResponse(content=resp_body, status_code=status or 200, headers=out_headers)
        return Response(
            content=str(resp_body), status_code=status or 200, headers=out_headers
        )


class OllamaStreamHandler:
    @staticmethod
    async def handle(request: Request, path: str) -> Response:
        ctx = HandlerContext(request)
        body = await _read_json_body(request)
        route = _resolve_from_body(ctx, body)

        upstream_protocol = route.upstream.protocol

        if upstream_protocol == "openai":
            # ── Translate Ollama → OpenAI ─────────────────────────────────
            if "generate" in path:
                forward_body = _tr.req_ollama_generate_to_openai(body)
            else:
                forward_body = _tr.req_ollama_chat_to_openai(body)
            forward_body["model"] = route.upstream_model
            forward_body["stream"] = True
            upstream_path = _openai_path_for(path)
        else:
            # ── Native Ollama pass-through ────────────────────────────────
            forward_body = dict(body)
            forward_body["model"] = route.upstream_model
            forward_body["stream"] = True
            upstream_path = _ollama_chat_path(path)

        content = json.dumps(forward_body).encode("utf-8")
        headers = merge_outbound_headers(
            request.headers,
            route.upstream.headers,
            _auth_headers(route.upstream.api_key),
        )
        headers["content-type"] = "application/json"
        if upstream_protocol == "openai":
            headers.setdefault("accept", "text/event-stream")

        started = now()

        # ── Open stream; check status before entering generator ──────────
        resp = await ctx.forwarder.start_stream(
            upstream=route.upstream,
            method="POST",
            path=upstream_path,
            headers=headers,
            content=content,
        )

        if resp.status_code >= 400:
            err_bytes = await resp.aread()
            await resp.aclose()
            try:
                err_body: Any = json.loads(err_bytes)
            except Exception:
                err_body = err_bytes.decode("utf-8", errors="replace")
            resp_headers = dict(resp.headers)
            finished = now()
            _rlog.warning("llm_router", f"[ollama] {route.alias} upstream {resp.status_code}: {err_body}")
            await record_call(
                ctx=ctx, route=route, method="POST",
                url=str(route.upstream.base_url) + upstream_path,
                request_headers=headers, request_params=None,
                request_body=forward_body,
                response_status=resp.status_code,
                response_headers=resp_headers,
                response_body=err_body,
                started_at=started, first_byte_at=finished, finished_at=finished,
                stream=True, usage=(None, None, None), error=None,
            )
            out_headers = filter_response_headers(resp_headers)
            if isinstance(err_body, (dict, list)):
                return JSONResponse(content=err_body, status_code=resp.status_code, headers=out_headers)
            return Response(content=str(err_body), status_code=resp.status_code, headers=out_headers)

        # ── Status is 2xx: proceed with streaming generator ──────────────
        captured: list[str] = []
        first_byte_holder: dict[str, float | None] = {"t": None}

        if upstream_protocol == "openai":
            # ── SSE → NDJSON conversion generator ────────────────────────
            async def gen_openai() -> AsyncIterator[bytes]:
                status: int | None = None
                resp_headers: dict[str, str] | None = None
                error: str | None = None
                done_seen = False
                try:
                    status = resp.status_code
                    resp_headers = dict(resp.headers)
                    async for raw in resp.aiter_bytes():
                        if first_byte_holder["t"] is None:
                            first_byte_holder["t"] = now()
                        text = raw.decode("utf-8", errors="replace")
                        captured.append(text)
                        ndjson_lines = _tr.sse_chunk_to_ndjson_lines(text, route.alias)
                        for line in ndjson_lines:
                            if b'"done": true' in line or b'"done":true' in line:
                                done_seen = True
                            yield line
                except Exception as exc:
                    error = type(exc).__name__
                    _rlog.error("llm_router", f"[ollama→openai] {route.alias} stream failed: {exc}")
                    yield (json.dumps({"error": str(exc), "done": True}) + "\n").encode("utf-8")
                finally:
                    await resp.aclose()
                    if not done_seen:
                        yield _tr.make_done_ndjson(route.alias, captured)
                    finished = now()
                    usage = usage_extract.extract_openai_stream(captured)
                    if status is not None and status >= 400:
                        _rlog.warning(
                            "llm_router",
                            f"[ollama→openai] {route.alias} upstream stream {status}: {''.join(captured)[:500]}",
                        )
                    import asyncio as _aio
                    merged = merge_openai_stream(captured) if not error else None
                    _aio.create_task(
                        record_call(
                            ctx=ctx,
                            route=route,
                            method="POST",
                            url=str(route.upstream.base_url) + upstream_path,
                            request_headers=headers,
                            request_params=None,
                            request_body=forward_body,
                            response_status=status,
                            response_headers=resp_headers,
                            response_body=captured,
                            started_at=started,
                            first_byte_at=first_byte_holder["t"],
                            finished_at=finished,
                            stream=True,
                            usage=usage,
                            error=error,
                            merged_body=merged,
                        )
                    )

            return StreamingResponse(
                gen_openai(),
                media_type="application/x-ndjson",
                headers={"cache-control": "no-cache"},
            )

        else:
            # ── Native Ollama NDJSON pass-through ─────────────────────────
            async def gen_ollama() -> AsyncIterator[bytes]:
                status: int | None = None
                resp_headers: dict[str, str] | None = None
                error: str | None = None
                try:
                    status = resp.status_code
                    resp_headers = dict(resp.headers)
                    async for raw in resp.aiter_bytes():
                        if first_byte_holder["t"] is None:
                            first_byte_holder["t"] = now()
                        try:
                            captured.append(raw.decode("utf-8", errors="replace"))
                        except Exception:
                            pass
                        yield raw
                except Exception as exc:
                    error = type(exc).__name__
                    _rlog.error("llm_router", f"[ollama] {route.alias} stream failed: {exc}")
                    yield (json.dumps({"error": str(exc)}) + "\n").encode("utf-8")
                finally:
                    await resp.aclose()
                    finished = now()
                    usage = usage_extract.extract_ollama_stream(captured)
                    if status is not None and status >= 400:
                        _rlog.warning(
                            "llm_router",
                            f"[ollama] {route.alias} upstream stream {status}: {''.join(captured)[:500]}",
                        )
                    import asyncio as _aio
                    merged = merge_ollama_stream(captured) if not error else None
                    _aio.create_task(
                        record_call(
                            ctx=ctx,
                            route=route,
                            method="POST",
                            url=str(route.upstream.base_url) + upstream_path,
                            request_headers=headers,
                            request_params=None,
                            request_body=forward_body,
                            response_status=status,
                            response_headers=resp_headers,
                            response_body=captured,
                            started_at=started,
                            first_byte_at=first_byte_holder["t"],
                            finished_at=finished,
                            stream=True,
                            usage=usage,
                            error=error,
                            merged_body=merged,
                        )
                    )

            return StreamingResponse(
                gen_ollama(),
                media_type="application/x-ndjson",
                headers={"cache-control": "no-cache"},
            )

