"""Stream chunk → complete response-body merger.

Each protocol's stream handler captures raw chunks as `List[str]`.
These functions reconstruct the equivalent unary response body from
those chunks, so that a `.merged.json` file can present the full
generated content in a human-readable, protocol-native shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# OpenAI
# ═══════════════════════════════════════════════════════════════════════════

def _parse_openai_sse_events(chunk: str) -> list[dict[str, Any]]:
    """Parse a raw SSE chunk (may contain multiple ``data:`` lines) into dicts.

    Each event block is separated by ``\\n\\n``.  ``data: [DONE]`` lines are
    skipped.
    """
    events: list[dict[str, Any]] = []
    for block in chunk.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    return events


def merge_openai_stream(chunks: list[str]) -> dict[str, Any]:
    """Reconstruct an OpenAI ``chat.completion`` response body from SSE chunks.

    Each chunk string may contain multiple ``data:`` lines.  Supported
    delta fields:

    - ``content``           → accumulate into message.content
    - ``reasoning_content`` → accumulate into message.reasoning_content
    - ``tool_calls``        → accumulate function name + arguments per index
    - ``role``              → set message.role (first chunk only)
    """
    result: dict[str, Any] = {
        "id": None,
        "object": "chat.completion",
        "created": None,
        "model": None,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ],
        "usage": None,
    }

    # Per-index accumulators for tool_calls.
    tool_names: dict[int, str] = {}
    tool_args_acc: dict[int, str] = {}

    # ── Step 1: flatten chunks → event list ──────────────────────────
    all_events: list[dict[str, Any]] = []
    for ch in chunks:
        if isinstance(ch, str):
            events = _parse_openai_sse_events(ch)
            all_events.extend(events)

    # ── Step 2: process events in order ──────────────────────────────
    target_msg = result["choices"][0]["message"]
    target_choice = result["choices"][0]

    for data in all_events:
        if not isinstance(data, dict):
            continue

        # Top-level metadata (first occurrence wins).
        if result["id"] is None:
            result["id"] = data.get("id")
        if result["created"] is None:
            result["created"] = data.get("created")
        if result["model"] is None:
            result["model"] = data.get("model")

        choices = data.get("choices") or []
        for c in choices:
            if not isinstance(c, dict):
                continue
            delta = c.get("delta") or {}
            if isinstance(delta, dict):
                # Role
                if "role" in delta:
                    target_msg["role"] = delta["role"]
                # Content
                content = delta.get("content")
                if isinstance(content, str) and content:
                    target_msg["content"] += content
                # Reasoning content (OpenAI o-series models)
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    target_msg.setdefault("reasoning_content", "")
                    target_msg["reasoning_content"] += reasoning
                # Tool calls
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = tc.get("index", 0)
                    # Initialise tool_calls list slot.
                    if "tool_calls" not in target_msg or target_msg["tool_calls"] is None:
                        target_msg["tool_calls"] = []
                    while len(target_msg["tool_calls"]) <= idx:
                        target_msg["tool_calls"].append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                    tc_obj = target_msg["tool_calls"][idx]
                    if "id" in tc:
                        tc_obj["id"] = tc["id"]
                    if "type" in tc:
                        tc_obj["type"] = tc["type"]
                    func_delta = tc.get("function") or {}
                    if isinstance(func_delta, dict):
                        if "name" in func_delta:
                            tool_names[idx] = tool_names.get(idx, "") + func_delta.get("name", "")
                            tc_obj["function"]["name"] = tool_names[idx]
                        if "arguments" in func_delta:
                            tool_args_acc[idx] = tool_args_acc.get(idx, "") + func_delta.get("arguments", "")
                            tc_obj["function"]["arguments"] = tool_args_acc[idx]

            # Finish reason
            fr = c.get("finish_reason")
            if fr:
                target_choice["finish_reason"] = fr

        # Usage (last chunk carries the final values).
        usage = data.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage

    # ── Step 3: post-process — parse tool_use arguments JSON ─────────
    if target_msg.get("tool_calls"):
        for tc in target_msg["tool_calls"]:
            raw_args = tc["function"].get("arguments", "")
            if isinstance(raw_args, str) and raw_args:
                try:
                    tc["function"]["arguments"] = json.loads(raw_args)
                except json.JSONDecodeError:
                    pass  # keep raw string if JSON is incomplete

    # Clean up empty tool_calls.
    if target_msg.get("tool_calls") == []:
        target_msg.pop("tool_calls", None)

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Anthropic
# ═══════════════════════════════════════════════════════════════════════════

def _parse_anthropic_sse_events(chunk: str) -> list[dict[str, Any]]:
    """Parse a raw SSE chunk (may contain multiple events) into event dicts.

    Each event block is separated by ``\\n\\n`` and each block contains
    ``event: <type>`` and ``data: <json>`` lines.
    """
    events: list[dict[str, Any]] = []
    for block in chunk.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type: str | None = None
        data_str: str | None = None
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()
        if event_type and data_str:
            try:
                events.append({"event": event_type, "data": json.loads(data_str)})
            except json.JSONDecodeError:
                pass
    return events


def merge_anthropic_stream(chunks: list[str]) -> dict[str, Any]:
    """Reconstruct an Anthropic ``/v1/messages`` response body from SSE chunks.

    Each chunk string may contain multiple SSE events (``event:`` / ``data:``
    pairs separated by ``\\n\\n``).  Supported event types and delta types:

    - ``message_start`` → id, model, role, usage.input_tokens
    - ``content_block_start`` → content_block index + type
    - ``content_block_delta`` →
        * ``text_delta``        → accumulate text
        * ``thinking_delta``    → accumulate thinking
        * ``signature_delta``   → accumulate signature
        * ``input_json_delta``  → accumulate partial_json (tool_use)
    - ``content_block_stop``   → finalise block (parse tool_use input JSON)
    - ``message_delta``        → stop_reason, usage.output_tokens
    """
    result: dict[str, Any] = {
        "id": None,
        "type": "message",
        "role": "assistant",
        "model": None,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
    }

    # Per-block accumulators keyed by index.
    blocks: dict[int, dict[str, Any]] = {}
    # Accumulated partial_json strings for tool_use blocks.
    partial_json_acc: dict[int, str] = {}

    # ── Step 1: flatten chunks → event list ──────────────────────────
    all_events: list[dict[str, Any]] = []
    for ch in chunks:
        events = _parse_anthropic_sse_events(ch) if isinstance(ch, str) else []
        all_events.extend(events)

    # ── Step 2: process events in order ──────────────────────────────
    for ev in all_events:
        etype = ev.get("event")
        data = ev.get("data", {})

        if etype == "message_start":
            msg = data.get("message") or {}
            if isinstance(msg, dict):
                result["id"] = result["id"] or msg.get("id")
                result["model"] = result["model"] or msg.get("model")
                result["role"] = msg.get("role", result["role"])
                usage = msg.get("usage") or {}
                if isinstance(usage, dict):
                    result["usage"]["input_tokens"] = usage.get("input_tokens", 0)
                    result["usage"]["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0)
                    result["usage"]["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0)

        elif etype == "content_block_start":
            idx = data.get("index", 0)
            cb = data.get("content_block") or {}
            if isinstance(cb, dict):
                blocks[idx] = dict(cb)
            partial_json_acc[idx] = ""

        elif etype == "content_block_delta":
            idx = data.get("index", 0)
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            block = blocks.setdefault(idx, {"type": "text"})

            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")

            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")

            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")

            elif dtype == "input_json_delta":
                partial_json_acc[idx] = partial_json_acc.get(idx, "") + delta.get("partial_json", "")

        elif etype == "content_block_stop":
            idx = data.get("index", 0)
            block = blocks.get(idx)
            if block and block.get("type") == "tool_use":
                raw = partial_json_acc.get(idx, "")
                if raw:
                    try:
                        block["input"] = json.loads(raw)
                    except json.JSONDecodeError:
                        block["input"] = raw
                else:
                    block.setdefault("input", {})

        elif etype == "message_delta":
            d = data.get("delta") or {}
            if isinstance(d, dict):
                result["stop_reason"] = d.get("stop_reason", result["stop_reason"])
                result["stop_sequence"] = d.get("stop_sequence", result["stop_sequence"])
            usage = data.get("usage") or {}
            if isinstance(usage, dict):
                result["usage"]["output_tokens"] = usage.get("output_tokens", 0)

        elif etype == "message_stop":
            pass  # no-op — stream end marker

    # ── Step 3: assemble content array ───────────────────────────────
    result["content"] = [blocks[i] for i in sorted(blocks.keys())]
    if not result["content"]:
        result["content"] = [{"type": "text", "text": ""}]
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Gemini
# ═══════════════════════════════════════════════════════════════════════════

def merge_gemini_stream(chunks: list[str]) -> dict[str, Any]:
    """Reconstruct a Gemini ``generateContent`` response body from stream chunks.

    Gemini streams newline-delimited JSON arrays (or bare JSON objects).
    Each chunk is itself a ``generateContent``-shaped object with
    incremental ``candidates[*].content.parts[*].text``.
    """
    merged: dict[int, dict[str, Any]] = {}  # candidate index → merged candidate
    usage_metadata: dict[str, Any] | None = None
    model_version: str | None = None

    for ch in chunks:
        data: dict[str, Any] | None = None
        if isinstance(ch, dict):
            data = ch
        elif isinstance(ch, (bytes, bytearray, str)):
            text = ch.decode("utf-8", errors="replace") if isinstance(ch, (bytes, bytearray)) else ch
            # Gemini may send a JSON array like [{...}, {...}] or single objects.
            text = text.strip()
            if not text:
                continue
            # Try array first.
            if text.startswith("["):
                try:
                    arr = json.loads(text)
                    if isinstance(arr, list):
                        for item in arr:
                            _merge_gemini_item(item, merged)
                        continue
                except json.JSONDecodeError:
                    pass
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Split on newlines and try each line.
                for line in text.splitlines():
                    line = line.strip().rstrip(",")
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            _merge_gemini_item(data, merged)
                    except json.JSONDecodeError:
                        continue
                continue
        if isinstance(data, dict):
            _merge_gemini_item(data, merged)

        # Pick up usageMetadata from the last chunk that has it.
        target = data if isinstance(data, dict) else None
        if target is None and isinstance(ch, dict):
            target = ch
        if target:
            um = target.get("usageMetadata")
            if isinstance(um, dict):
                usage_metadata = um
            mv = target.get("modelVersion")
            if mv:
                model_version = mv

    candidates = []
    for idx in sorted(merged.keys()):
        c = merged[idx]
        candidates.append({
            "content": c.get("content", {"role": "model", "parts": [{"text": ""}]}),
            "finishReason": c.get("finishReason"),
            "safetyRatings": c.get("safetyRatings"),
        })

    result: dict[str, Any] = {
        "candidates": candidates if candidates else [{"content": {"role": "model", "parts": [{"text": ""}]}}],
    }
    if usage_metadata:
        result["usageMetadata"] = usage_metadata
    if model_version:
        result["modelVersion"] = model_version
    return result


def _merge_gemini_item(item: dict[str, Any], merged: dict[int, dict[str, Any]]) -> None:
    """Merge a single Gemini response object into the per-candidate accumulator."""
    if not isinstance(item, dict):
        return
    for c in item.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        idx = c.get("index", 0)
        if idx not in merged:
            merged[idx] = {"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": None, "safetyRatings": None}

        src_content = c.get("content") or {}
        dst_parts = merged[idx]["content"].setdefault("parts", [{"text": ""}])

        for part in src_content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text", "")
            if text:
                if dst_parts and isinstance(dst_parts[-1], dict):
                    dst_parts[-1]["text"] = dst_parts[-1].get("text", "") + text
                else:
                    dst_parts.append({"text": text})

        fr = c.get("finishReason")
        if fr:
            merged[idx]["finishReason"] = fr
        sr = c.get("safetyRatings")
        if sr:
            merged[idx]["safetyRatings"] = sr


# ═══════════════════════════════════════════════════════════════════════════
# Ollama
# ═══════════════════════════════════════════════════════════════════════════

def merge_ollama_stream(chunks: list[str]) -> dict[str, Any]:
    """Reconstruct an Ollama ``/api/chat`` response body from NDJSON chunks.

    Each line is a JSON object; ``message.content`` is incremental.
    The final chunk has ``done: true`` and carries usage/timing fields.
    """
    result: dict[str, Any] = {
        "model": None,
        "created_at": None,
        "message": {"role": "assistant", "content": ""},
        "done": True,
    }
    for ch in chunks:
        text: str | None = None
        if isinstance(ch, (bytes, bytearray)):
            try:
                text = ch.decode("utf-8", errors="replace")
            except Exception:
                continue
        elif isinstance(ch, str):
            text = ch
        if not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if result["model"] is None:
                result["model"] = obj.get("model")
            if result["created_at"] is None:
                result["created_at"] = obj.get("created_at")
            msg = obj.get("message") or {}
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    result["message"]["content"] += content
            # Final chunk carries usage / timing.
            if obj.get("done"):
                for k in ("total_duration", "load_duration", "prompt_eval_count",
                          "eval_count", "prompt_eval_duration", "eval_duration"):
                    if k in obj:
                        result[k] = obj[k]
    return result
