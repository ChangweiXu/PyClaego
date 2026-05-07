"""Ollama-native inbound routes.

Path map:
- POST /api/chat          → chat completions
- POST /api/generate      → text generation
- POST /api/embed         → embeddings (current)
- POST /api/embeddings    → embeddings (legacy)
- POST /api/show          → model details (alias→upstream_model rewrite)
- GET  /api/tags          → list of configured ollama aliases
- GET  /api/version       → static version stub

Streaming rule: Ollama's clients default to streaming. We treat a missing
or truthy `stream` field as streaming; only an explicit `stream: false`
routes to the unary handler.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..handlers.ollama_handler import OllamaStreamHandler, OllamaUnaryHandler

router = APIRouter()


def _is_stream(body: dict[str, Any]) -> bool:
    """Ollama default is streaming; only `stream: false` opts out."""
    if "stream" not in body:
        return True
    return body.get("stream") is not False


async def _peek_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/api/chat")
async def chat(request: Request) -> Response:
    body = await _peek_body(request)
    if _is_stream(body):
        return await OllamaStreamHandler.handle(request, "/api/chat")
    return await OllamaUnaryHandler.handle(request, "/api/chat")


@router.post("/api/generate")
async def generate(request: Request) -> Response:
    body = await _peek_body(request)
    if _is_stream(body):
        return await OllamaStreamHandler.handle(request, "/api/generate")
    return await OllamaUnaryHandler.handle(request, "/api/generate")


@router.post("/api/embed")
async def embed(request: Request) -> Response:
    return await OllamaUnaryHandler.handle(request, "/api/embed")


@router.post("/api/embeddings")
async def embeddings(request: Request) -> Response:
    return await OllamaUnaryHandler.handle(request, "/api/embeddings")


@router.post("/api/show")
async def show(request: Request) -> Response:
    """Return model metadata.

    - ``protocol: ollama`` upstream: forward to the real daemon (it has modelfile/template).
    - ``protocol: openai`` upstream: synthesize metadata locally (no equivalent endpoint).
    """
    body = await _peek_body(request)
    alias = body.get("model") or body.get("name")
    if not alias:
        raise HTTPException(status_code=400, detail="specify 'model' or 'name'")

    routes = request.app.state.routes
    route = routes.resolve("ollama", alias) or routes.resolve("openai", alias)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": f"model '{alias}' not found"}},
        )

    if route.upstream.protocol == "ollama":
        # Real Ollama daemon supports /api/show — pass through.
        return await OllamaUnaryHandler.handle(request, "/api/show")

    # OpenAI-compatible upstream: synthesize metadata locally.
    return JSONResponse({
        "model": route.alias,
        "modified_at": "1970-01-01T00:00:00Z",
        "template": "",
        "parameters": "",
        "details": {
            "parent_model": route.upstream_model,
            "format": "openai-compat",
            "family": "",
            "families": [],
            "parameter_size": "",
            "quantization_level": "",
        },
    })


@router.get("/api/tags")
async def tags(request: Request) -> dict[str, Any]:
    """Return all models reachable via Ollama inbound protocol.

    Includes both:
    - ``protocol: ollama`` upstreams  (real Ollama daemons, pass-through)
    - ``protocol: openai`` upstreams  (OpenAI-compatible APIs, auto-translated)

    The ``details.format`` field indicates which type each entry is.
    """
    routes = request.app.state.routes
    models = []

    def _entry(r: Any, fmt: str) -> dict[str, Any]:
        return {
            "name": r.alias,
            "model": r.alias,
            "modified_at": "1970-01-01T00:00:00Z",
            "size": 0,
            "digest": "",
            "details": {
                "parent_model": r.upstream_model,
                "format": fmt,
                "family": "",
                "families": [],
                "parameter_size": "",
                "quantization_level": "",
            },
        }

    for r in routes.list_aliases(protocol="ollama"):
        models.append(_entry(r, "ollama"))
    for r in routes.list_aliases(protocol="openai"):
        models.append(_entry(r, "openai-compat"))

    return {"models": models}


@router.get("/api/version")
async def version() -> dict[str, Any]:
    return {"version": "0.22.1"}
