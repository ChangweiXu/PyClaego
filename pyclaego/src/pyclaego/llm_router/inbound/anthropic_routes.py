"""Anthropic-compatible inbound routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response

from ..handlers.anthropic_handler import AnthropicStreamHandler, AnthropicUnaryHandler

router = APIRouter()


async def _peek_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


@router.post("/v1/messages")
async def messages(request: Request) -> Response:
    body = await _peek_body(request)
    if body.get("stream") is True:
        return await AnthropicStreamHandler.handle(request, "/v1/messages")
    return await AnthropicUnaryHandler.handle(request, "/v1/messages")
