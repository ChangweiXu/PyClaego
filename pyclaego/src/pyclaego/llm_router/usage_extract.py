"""Extract token usage from upstream response bodies (per-protocol).

Returns (prompt_tokens, completion_tokens, total_tokens), each Optional[int].

For streaming responses, the input is expected to be a list of decoded
chunks (dicts for JSON-stream / Anthropic / Gemini, strings for raw SSE
data lines from OpenAI). The functions defensively accept missing fields.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

Usage = tuple[int | None, int | None, int | None]


def _u(p: Any, c: Any, t: Any) -> Usage:
    def _i(x: Any) -> int | None:
        try:
            return int(x) if x is not None else None
        except (TypeError, ValueError):
            return None
    return (_i(p), _i(c), _i(t))


# ---------- OpenAI ----------

def extract_openai_unary(body: Any) -> Usage:
    if not isinstance(body, dict):
        return (None, None, None)
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        return (None, None, None)
    return _u(
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )


def extract_openai_stream(chunks: list[Any]) -> Usage:
    """Look for a chunk with `usage` (sent when stream_options.include_usage=true)."""
    for ch in reversed(chunks):
        data = _parse_sse_chunk(ch)
        if isinstance(data, dict):
            usage = data.get("usage")
            if isinstance(usage, dict):
                return _u(
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    usage.get("total_tokens"),
                )
    return (None, None, None)


def _parse_sse_chunk(chunk: Any) -> Any:
    """SSE chunks may be raw bytes/str like 'data: {...}\\n\\n' or already-parsed."""
    if isinstance(chunk, dict):
        return chunk
    if isinstance(chunk, (bytes, bytearray)):
        try:
            chunk = chunk.decode("utf-8", errors="replace")
        except Exception:
            return None
    if not isinstance(chunk, str):
        return None
    # Typical SSE line: "data: {...}"
    for line in chunk.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue
    return None


# ---------- Anthropic ----------

def extract_anthropic_unary(body: Any) -> Usage:
    if not isinstance(body, dict):
        return (None, None, None)
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        return (None, None, None)
    p = usage.get("input_tokens")
    c = usage.get("output_tokens")
    t = None
    if isinstance(p, int) and isinstance(c, int):
        t = p + c
    return _u(p, c, t)


def extract_anthropic_stream(chunks: list[Any]) -> Usage:
    """Anthropic streams `message_start` (input_tokens) and `message_delta` (output_tokens, cumulative)."""
    p_total: int | None = None
    c_total: int | None = None
    for ch in chunks:
        data = _parse_sse_chunk(ch)
        if not isinstance(data, dict):
            continue
        ev_type = data.get("type")
        if ev_type == "message_start":
            msg = data.get("message") or {}
            if isinstance(msg, dict):
                u = msg.get("usage") or {}
                if isinstance(u, dict):
                    p_total = u.get("input_tokens", p_total)
                    c_total = u.get("output_tokens", c_total)
        elif ev_type == "message_delta":
            u = data.get("usage") or {}
            if isinstance(u, dict):
                if "input_tokens" in u:
                    p_total = u.get("input_tokens", p_total)
                if "output_tokens" in u:
                    c_total = u.get("output_tokens", c_total)
    t = None
    if isinstance(p_total, int) and isinstance(c_total, int):
        t = p_total + c_total
    return _u(p_total, c_total, t)


# ---------- Gemini ----------

def extract_gemini_unary(body: Any) -> Usage:
    if not isinstance(body, dict):
        return (None, None, None)
    meta = body.get("usageMetadata") or {}
    if not isinstance(meta, dict):
        return (None, None, None)
    return _u(
        meta.get("promptTokenCount"),
        meta.get("candidatesTokenCount"),
        meta.get("totalTokenCount"),
    )


def extract_gemini_stream(chunks: list[Any]) -> Usage:
    """Gemini stream emits a JSON array; usageMetadata appears in the final chunk."""
    for ch in reversed(chunks):
        # In stream mode, each chunk is itself a JSON object (not SSE wrapped).
        if isinstance(ch, dict):
            data = ch
        elif isinstance(ch, (bytes, bytearray, str)):
            try:
                text = ch.decode("utf-8") if isinstance(ch, (bytes, bytearray)) else ch
                data = json.loads(text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        else:
            continue
        if isinstance(data, dict):
            meta = data.get("usageMetadata")
            if isinstance(meta, dict):
                return _u(
                    meta.get("promptTokenCount"),
                    meta.get("candidatesTokenCount"),
                    meta.get("totalTokenCount"),
                )
    return (None, None, None)


# ---------- Ollama ----------

def _ollama_usage_from_obj(obj: Any) -> Usage | None:
    """Return usage tuple from an Ollama response object, or None if absent."""
    if not isinstance(obj, dict):
        return None
    p = obj.get("prompt_eval_count")
    c = obj.get("eval_count")
    if p is None and c is None:
        return None
    t: int | None = None
    if isinstance(p, int) and isinstance(c, int):
        t = p + c
    return _u(p, c, t)


def extract_ollama_unary(body: Any) -> Usage:
    found = _ollama_usage_from_obj(body)
    return found if found is not None else (None, None, None)


def extract_ollama_stream(chunks: list[Any]) -> Usage:
    """Ollama streams NDJSON; usage fields appear on the final chunk (`done: true`)."""
    last: Usage | None = None
    text_parts: list[str] = []
    for ch in chunks:
        if isinstance(ch, (bytes, bytearray)):
            try:
                text_parts.append(ch.decode("utf-8", errors="replace"))
            except Exception:
                continue
        elif isinstance(ch, str):
            text_parts.append(ch)
        elif isinstance(ch, dict):
            found = _ollama_usage_from_obj(ch)
            if found is not None:
                last = found
    full = "".join(text_parts)
    for line in full.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _ollama_usage_from_obj(obj)
        if found is not None:
            last = found
    return last if last is not None else (None, None, None)
