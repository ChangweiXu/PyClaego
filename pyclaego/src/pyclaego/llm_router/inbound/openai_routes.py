"""OpenAI-compatible inbound routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from ..handlers.openai_handler import OpenAIStreamHandler, OpenAIUnaryHandler

router = APIRouter()


def _is_stream(body: dict[str, Any]) -> bool:
    val = body.get("stream")
    return bool(val) is True and val is not None


async def _peek_body(request: Request) -> dict[str, Any]:
    """Read the body once and stash it on the request so handlers can re-read."""
    raw = await request.body()
    # FastAPI Request caches body; subsequent .json() will reuse it.
    if not raw:
        return {}
    try:
        import json
        return json.loads(raw)
    except Exception:
        return {}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body = await _peek_body(request)
    if _is_stream(body):
        return await OpenAIStreamHandler.handle(request, "/chat/completions")
    return await OpenAIUnaryHandler.handle(request, "/chat/completions")


@router.post("/v1/completions")
async def completions(request: Request) -> Response:
    body = await _peek_body(request)
    if _is_stream(body):
        return await OpenAIStreamHandler.handle(request, "/completions")
    return await OpenAIUnaryHandler.handle(request, "/completions")


@router.post("/v1/embeddings")
async def embeddings(request: Request) -> Response:
    # Embeddings are never streaming.
    return await OpenAIUnaryHandler.handle(request, "/embeddings")


@router.get("/v1/models")
async def models(request: Request) -> dict[str, Any]:
    routes = request.app.state.routes
    data = []
    for r in routes.list_aliases(protocol="openai"):
        data.append(
            {
                "id": r.alias,
                "object": "model",
                "owned_by": r.upstream.id,
            }
        )
    return {"object": "list", "data": data}
