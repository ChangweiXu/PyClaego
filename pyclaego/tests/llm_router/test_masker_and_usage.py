"""Tests for masker and usage_extract."""

from __future__ import annotations

from pyclaego.llm_router import usage_extract
from pyclaego.llm_router.recording.masker import (
    REDACTED,
    mask_body,
    mask_headers,
    mask_query_params,
)


def test_mask_headers_redacts_authorization_always():
    out = mask_headers({"Authorization": "Bearer x", "X-Other": "ok"}, [])
    assert out["Authorization"] == REDACTED
    assert out["X-Other"] == "ok"


def test_mask_headers_redacts_configured_keys_case_insensitive():
    out = mask_headers(
        {"x-api-key": "a", "X-Goog-Api-Key": "b", "X-Other": "c"},
        ["x-api-key", "x-goog-api-key"],
    )
    assert out["x-api-key"] == REDACTED
    assert out["X-Goog-Api-Key"] == REDACTED
    assert out["X-Other"] == "c"


def test_mask_query_params():
    out = mask_query_params({"key": "secret", "other": "k"}, ["key"])
    assert out["key"] == REDACTED
    assert out["other"] == "k"


def test_mask_body_recursive():
    body = {
        "model": "x",
        "api_key": "secret",
        "nested": {"Authorization": "tok", "ok": 1},
        "list": [{"api_key": "z"}, {"keep": 2}],
    }
    out = mask_body(body, ["api_key", "authorization"])
    assert out["api_key"] == REDACTED
    assert out["nested"]["Authorization"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert out["list"][0]["api_key"] == REDACTED
    assert out["list"][1]["keep"] == 2
    # original untouched (deep copy)
    assert body["api_key"] == "secret"


# ---------- usage_extract ----------

def test_extract_openai_unary():
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
    assert usage_extract.extract_openai_unary(body) == (10, 20, 30)


def test_extract_openai_unary_missing():
    assert usage_extract.extract_openai_unary({}) == (None, None, None)


def test_extract_openai_stream_from_sse_chunks():
    chunks = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":3,"completion_tokens":5,"total_tokens":8}}\n\n',
        "data: [DONE]\n\n",
    ]
    assert usage_extract.extract_openai_stream(chunks) == (3, 5, 8)


def test_extract_anthropic_unary():
    body = {"usage": {"input_tokens": 4, "output_tokens": 7}}
    assert usage_extract.extract_anthropic_unary(body) == (4, 7, 11)


def test_extract_anthropic_stream():
    chunks = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n',
        'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n',
        'data: {"type":"message_delta","usage":{"output_tokens":12}}\n\n',
    ]
    assert usage_extract.extract_anthropic_stream(chunks) == (5, 12, 17)


def test_extract_gemini_unary():
    body = {
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 9,
            "totalTokenCount": 12,
        }
    }
    assert usage_extract.extract_gemini_unary(body) == (3, 9, 12)


def test_extract_gemini_stream():
    import json
    chunks = [
        json.dumps({"candidates": [{"content": {"parts": [{"text": "a"}]}}]}),
        json.dumps(
            {
                "candidates": [{"content": {"parts": [{"text": "b"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 6,
                },
            }
        ),
    ]
    assert usage_extract.extract_gemini_stream(chunks) == (2, 4, 6)


def test_extract_ollama_unary():
    body = {
        "model": "x",
        "done": True,
        "prompt_eval_count": 11,
        "eval_count": 22,
    }
    assert usage_extract.extract_ollama_unary(body) == (11, 22, 33)


def test_extract_ollama_unary_missing():
    assert usage_extract.extract_ollama_unary({"done": True}) == (None, None, None)


def test_extract_ollama_stream_ndjson():
    import json as _j
    chunks = [
        _j.dumps({"model": "x", "message": {"content": "hi"}, "done": False}) + "\n",
        _j.dumps(
            {
                "model": "x",
                "message": {"content": ""},
                "done": True,
                "prompt_eval_count": 4,
                "eval_count": 9,
            }
        )
        + "\n",
    ]
    assert usage_extract.extract_ollama_stream(chunks) == (4, 9, 13)


def test_extract_ollama_stream_split_across_chunks():
    """A single NDJSON line may arrive split across two byte chunks."""
    final = (
        '{"done":true,"prompt_eval_count":2,"eval_count":3}\n'
    )
    chunks = [
        '{"done":false}\n',
        final[:20],
        final[20:],
    ]
    assert usage_extract.extract_ollama_stream(chunks) == (2, 3, 5)
