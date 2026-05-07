"""Pure-function translators between Ollama and OpenAI wire formats.

Ollama (/api/chat, /api/generate) → OpenAI (/v1/chat/completions)
OpenAI response → Ollama response

No FastAPI or httpx imports; these are plain data transformers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# ─────────────────────────────── Request ────────────────────────────────────


def req_ollama_chat_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Ollama /api/chat body to an OpenAI /v1/chat/completions body.

    Fields mapped:
      messages             → messages  (identical format)
      options.temperature  → temperature
      options.top_p        → top_p
      options.num_predict  → max_tokens
      options.seed         → seed
      tools                → tools (passed through)
      format (when "json") → response_format: {"type": "json_object"}

    `stream` and `model` are NOT set here — the caller sets them.
    """
    out: dict[str, Any] = {}

    out["messages"] = body.get("messages") or []

    options = body.get("options") or {}
    if isinstance(options, dict):
        if "temperature" in options:
            out["temperature"] = options["temperature"]
        if "top_p" in options:
            out["top_p"] = options["top_p"]
        if "num_predict" in options:
            out["max_tokens"] = options["num_predict"]
        if "seed" in options:
            out["seed"] = options["seed"]

    if body.get("tools"):
        out["tools"] = body["tools"]

    if body.get("format") == "json":
        out["response_format"] = {"type": "json_object"}

    return out


def req_ollama_generate_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Ollama /api/generate body to an OpenAI /v1/chat/completions body.

    Wraps `prompt` as a single user message. `system` becomes a system message
    prepended before it.
    """
    out = req_ollama_chat_to_openai(body)

    # /api/generate uses `prompt` + optional `system` instead of `messages`.
    messages: list[dict[str, str]] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    prompt = body.get("prompt", "")
    messages.append({"role": "user", "content": prompt})
    out["messages"] = messages

    return out


# ─────────────────────────────── Response ───────────────────────────────────


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def resp_openai_to_ollama(openai_body: dict[str, Any], alias: str) -> dict[str, Any]:
    """Translate a non-streaming OpenAI chat response to Ollama format."""
    choices = openai_body.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"

    usage = openai_body.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    out: dict[str, Any] = {
        "model": alias,
        "created_at": _iso_now(),
        "message": {
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
        },
        "done": True,
        "done_reason": finish_reason,
    }
    if prompt_tokens is not None:
        out["prompt_eval_count"] = prompt_tokens
    if completion_tokens is not None:
        out["eval_count"] = completion_tokens
    if prompt_tokens is not None and completion_tokens is not None:
        out["eval_duration"] = 0  # unknown; clients tolerate 0
    return out


# ─────────────────────────── SSE → NDJSON streaming ─────────────────────────


def sse_chunk_to_ndjson_lines(raw: str, alias: str) -> list[bytes]:
    """Convert one raw SSE payload string to zero or more Ollama NDJSON line bytes.

    `raw` is whatever arrives from the upstream in one streaming chunk — it may
    contain multiple `data: …` lines.  Each complete, non-DONE data line is
    parsed and converted.

    Returns a list of UTF-8 encoded NDJSON lines (each ending in ``\\n``).
    """
    out: list[bytes] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        content = delta.get("content") or ""
        role = delta.get("role", "assistant")

        if finish_reason:
            # Final chunk — emit done:true with usage if present.
            usage = chunk.get("usage") or {}
            final: dict[str, Any] = {
                "model": alias,
                "created_at": _iso_now(),
                "message": {"role": role, "content": content},
                "done": True,
                "done_reason": finish_reason,
            }
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
            if pt is not None:
                final["prompt_eval_count"] = pt
            if ct is not None:
                final["eval_count"] = ct
            out.append((json.dumps(final) + "\n").encode("utf-8"))
        else:
            ndjson_obj: dict[str, Any] = {
                "model": alias,
                "created_at": _iso_now(),
                "message": {"role": role, "content": content},
                "done": False,
            }
            out.append((json.dumps(ndjson_obj) + "\n").encode("utf-8"))

    return out


def make_done_ndjson(alias: str, captured_sse: list[str]) -> bytes:
    """Produce a final ``done:true`` NDJSON line when the upstream didn't include one.

    Parses usage from captured SSE chunks (for upstreams that send usage only
    in the last chunk or via stream_options).
    """
    from .. import usage_extract  # local import to avoid circular at module level

    p, c, _ = usage_extract.extract_openai_stream(captured_sse)
    final: dict[str, Any] = {
        "model": alias,
        "created_at": _iso_now(),
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
    }
    if p is not None:
        final["prompt_eval_count"] = p
    if c is not None:
        final["eval_count"] = c
    return (json.dumps(final) + "\n").encode("utf-8")
