"""Gemini-compatible inbound routes.

Gemini uses `:generateContent` and `:streamGenerateContent` action suffixes.
We accept both the model name and the action via a single path param using
a regex match (FastAPI supports `{param:path}`-like via Starlette but we
use two explicit endpoints for clarity).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from ..handlers.gemini_handler import GeminiStreamHandler, GeminiUnaryHandler

router = APIRouter()


def _split_model_action(spec: str) -> tuple[str, str]:
    """Split 'model:action' (e.g. 'gemini-2.5-pro:generateContent')."""
    if ":" not in spec:
        raise HTTPException(
            status_code=400,
            detail="path must be /v1beta/models/{model}:{action}",
        )
    model, action = spec.split(":", 1)
    return model, action


@router.post("/v1beta/models/{model_action}")
async def gemini_generate(model_action: str, request: Request) -> Response:
    model, action = _split_model_action(model_action)
    if action == "generateContent":
        return await GeminiUnaryHandler.handle(request, model, action)
    if action == "streamGenerateContent":
        return await GeminiStreamHandler.handle(request, model, action)
    raise HTTPException(
        status_code=404,
        detail=f"unsupported gemini action ':{action}'",
    )
