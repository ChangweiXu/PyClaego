"""OpenAI-protocol handlers (unary + streaming SSE pass-through)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import usage_extract
from ..forwarder import filter_response_headers
from ..recording.stream_merger import merge_openai_stream
from ..routing import ResolvedRoute
from .base import HandlerContext, _rlog, merge_outbound_headers, now, record_call, resolve_or_404

PROTOCOL = "openai"


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


def _resolve_from_body(ctx: HandlerContext, body: dict[str, Any]) -> ResolvedRoute:
    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise HTTPException(status_code=400, detail="missing 'model' in request body")
    return resolve_or_404(ctx.routes, PROTOCOL, alias)


class OpenAIUnaryHandler:
    @staticmethod
    async def handle(request: Request, path: str) -> Response:
        ctx = HandlerContext(request)
        body = await _read_json_body(request)
        route = _resolve_from_body(ctx, body)

        forward_body = dict(body)
        forward_body["model"] = route.upstream_model
        # Force stream off (handler choice).
        forward_body.pop("stream", None)
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
                path=path,
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
            usage = usage_extract.extract_openai_unary(resp_body)
            if status >= 400:
                _rlog.warning("llm_router", f"[openai] {route.alias} upstream {status}: {resp_body}")
        except Exception as exc:
            error = type(exc).__name__
            _rlog.exception("llm_router", f"[openai] unary forward failed for {route.alias}: {exc}")
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
            url=str(route.upstream.base_url) + path,
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


class OpenAIStreamHandler:
    @staticmethod
    async def handle(request: Request, path: str) -> Response:
        ctx = HandlerContext(request)
        body = await _read_json_body(request)
        route = _resolve_from_body(ctx, body)

        forward_body = dict(body)
        forward_body["model"] = route.upstream_model
        forward_body["stream"] = True
        content = json.dumps(forward_body).encode("utf-8")

        headers = merge_outbound_headers(
            request.headers,
            route.upstream.headers,
            _auth_headers(route.upstream.api_key),
        )
        headers["content-type"] = "application/json"
        headers.setdefault("accept", "text/event-stream")

        started = now()

        # ── Open stream; check status before entering generator ──────────
        resp = await ctx.forwarder.start_stream(
            upstream=route.upstream,
            method="POST",
            path=path,
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
            _rlog.warning("llm_router", f"[openai] {route.alias} upstream {resp.status_code}: {err_body}")
            await record_call(
                ctx=ctx, route=route, method="POST",
                url=str(route.upstream.base_url) + path,
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

        async def gen() -> AsyncIterator[bytes]:
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
                _rlog.error("llm_router", f"[openai] {route.alias} stream failed: {exc}")
                yield (f'data: {{"error":"{exc}"}}\n\n').encode()
            finally:
                await resp.aclose()
                finished = now()
                usage = usage_extract.extract_openai_stream(captured)
                if status is not None and status >= 400:
                    _rlog.warning(
                        "llm_router",
                        f"[openai] {route.alias} upstream stream {status}: {''.join(captured)[:500]}",
                    )
                # Fire-and-forget recording.
                import asyncio as _aio
                merged = merge_openai_stream(captured) if not error else None
                _aio.create_task(
                    record_call(
                        ctx=ctx,
                        route=route,
                        method="POST",
                        url=str(route.upstream.base_url) + path,
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
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )
