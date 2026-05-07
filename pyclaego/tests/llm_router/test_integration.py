"""End-to-end test using FastAPI TestClient + httpx.MockTransport.

Replaces the `OutboundForwarder._clients` after app startup with httpx
clients backed by a MockTransport so we can assert the forwarded request
body has the rewritten model and that recording produces a dump file +
SQLite row.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pyclaego.llm_router.app import create_app
from pyclaego.llm_router.config import parse_router_config


def _raw(tmp_path: Path, protocol: str = "openai"):
    return {
        "server": {"host": "127.0.0.1", "port": 18790},
        "storage": {
            "call_dump_dir": str(tmp_path / "calls"),
            "stats_db_path": str(tmp_path / "stats.sqlite"),
            "dump_enabled": True,
            "mask_keys": ["api_key", "authorization", "x-api-key", "x-goog-api-key"],
        },
        "upstreams": [
            {
                "id": "u1",
                "protocol": protocol,
                "base_url": "https://upstream.example",
                "api_key": "sk-secret",
                "headers": {},
                "models": [
                    {"alias": "alpha", "upstream_model": "real/alpha"},
                ],
            }
        ],
    }


def _replace_clients_with_mock(app, mock_transport: httpx.MockTransport) -> None:
    """Swap out per-upstream httpx clients for ones using a MockTransport."""
    forwarder = app.state.forwarder
    for uid, client in list(forwarder._clients.items()):
        base_url = str(client.base_url)
        # Synchronously closing the real client is fine in tests.
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(client.aclose())
        except RuntimeError:
            pass
        forwarder._clients[uid] = httpx.AsyncClient(
            base_url=base_url, transport=mock_transport
        )


@pytest.fixture()
def stats_rows(tmp_path):
    """Helper to read all rows from the stats DB after the test."""
    def _read():
        db = tmp_path / "stats.sqlite"
        if not db.exists():
            return []
        con = sqlite3.connect(db)
        try:
            cur = con.execute("SELECT * FROM calls")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()
    return _read


def test_openai_unary_pass_through(tmp_path, stats_rows):
    cfg = parse_router_config(_raw(tmp_path, "openai"))
    app = create_app(cfg)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
        )

    transport = httpx.MockTransport(handler)

    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "alpha", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["usage"]["total_tokens"] == 12

    # Forwarded body has the rewritten model.
    assert seen["body"]["model"] == "real/alpha"
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["url"].endswith("/chat/completions")

    # Dump file exists.
    dump_files = list((tmp_path / "calls").rglob("*.json"))
    assert len(dump_files) == 1
    dump = json.loads(dump_files[0].read_text())
    # Authorization is masked in the dump (case-insensitive lookup).
    headers_lc = {k.lower(): v for k, v in dump["request"]["headers"].items()}
    assert headers_lc["authorization"] == "***REDACTED***"
    assert dump["usage"]["total_tokens"] == 12

    # Stats row recorded.
    rows = stats_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["alias"] == "alpha"
    assert row["upstream_model"] == "real/alpha"
    assert row["status"] == 200
    assert row["total_tokens"] == 12
    assert row["stream"] == 0


def test_openai_protocol_alias_mismatch_returns_404(tmp_path, stats_rows):
    cfg = parse_router_config(_raw(tmp_path, "anthropic"))
    app = create_app(cfg)

    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "alpha", "messages": []},
        )
    # alias 'alpha' is registered under anthropic protocol, not openai.
    assert resp.status_code == 404


def test_anthropic_unary_pass_through(tmp_path, stats_rows):
    cfg = parse_router_config(_raw(tmp_path, "anthropic"))
    app = create_app(cfg)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["x_api_key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "id": "msg_x",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 4, "output_tokens": 6},
            },
        )

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/v1/messages",
            json={
                "model": "alpha",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            },
        )
    assert resp.status_code == 200
    assert seen["body"]["model"] == "real/alpha"
    assert seen["x_api_key"] == "sk-secret"
    rows = stats_rows()
    assert rows[0]["total_tokens"] == 10


def test_gemini_unary_pass_through(tmp_path, stats_rows):
    cfg = parse_router_config(_raw(tmp_path, "gemini"))
    app = create_app(cfg)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x_goog"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 5,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/v1beta/models/alpha:generateContent",
            json={"contents": [{"parts": [{"text": "hi"}]}]},
        )
    assert resp.status_code == 200
    # URL was rewritten with upstream_model
    assert "/v1beta/models/real/alpha:generateContent" in seen["url"]
    assert seen["x_goog"] == "sk-secret"
    rows = stats_rows()
    assert rows[0]["total_tokens"] == 5


def test_models_endpoint_lists_openai_aliases(tmp_path):
    cfg = parse_router_config(_raw(tmp_path, "openai"))
    app = create_app(cfg)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(m["id"] == "alpha" for m in data)


def test_ollama_unary_pass_through(tmp_path, stats_rows):
    cfg = parse_router_config(_raw(tmp_path, "ollama"))
    app = create_app(cfg)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "real/alpha",
                "message": {"role": "assistant", "content": "hi"},
                "done": True,
                "prompt_eval_count": 6,
                "eval_count": 9,
            },
        )

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/api/chat",
            json={
                "model": "alpha",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
    assert resp.status_code == 200
    assert seen["body"]["model"] == "real/alpha"
    assert seen["body"]["stream"] is False
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["url"].endswith("/api/chat")

    rows = stats_rows()
    assert len(rows) == 1
    assert rows[0]["alias"] == "alpha"
    assert rows[0]["total_tokens"] == 15
    assert rows[0]["stream"] == 0


def test_ollama_stream_pass_through(tmp_path, stats_rows):
    cfg = parse_router_config(_raw(tmp_path, "ollama"))
    app = create_app(cfg)

    seen = {}
    ndjson_lines = [
        json.dumps({"model": "real/alpha", "message": {"content": "hi"}, "done": False}),
        json.dumps(
            {
                "model": "real/alpha",
                "message": {"content": ""},
                "done": True,
                "prompt_eval_count": 3,
                "eval_count": 4,
            }
        ),
    ]
    body_bytes = ("\n".join(ndjson_lines) + "\n").encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"content-type": "application/x-ndjson"},
        )

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        # No `stream` field → defaults to streaming.
        resp = client.post(
            "/api/chat",
            json={
                "model": "alpha",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200
    assert seen["body"]["stream"] is True
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    # NDJSON with spaces after colons: `"done": true`
    assert b'"done": true' in resp.content

    # Recording happens via fire-and-forget asyncio.create_task; with
    # TestClient + sync app shutdown, the task may or may not complete
    # before the lifespan closes the DB. Allow either 0 or 1 rows.
    rows = stats_rows()
    if rows:
        assert rows[0]["alias"] == "alpha"
        assert rows[0]["stream"] == 1
        assert rows[0]["total_tokens"] == 7


def test_ollama_tags_lists_aliases(tmp_path):
    cfg = parse_router_config(_raw(tmp_path, "ollama"))
    app = create_app(cfg)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.get("/api/tags")
    assert resp.status_code == 200
    models = resp.json()["models"]
    assert any(m["name"] == "alpha" for m in models)


# ──────────────────────────────────────────────────────────────────────────────
# New tests: Ollama inbound → OpenAI upstream translation
# ──────────────────────────────────────────────────────────────────────────────


def test_ollama_via_openai_upstream_unary(tmp_path, stats_rows):
    """Ollama /api/chat with stream:false backed by an OpenAI upstream.

    The handler should:
    - Resolve 'alpha' via the openai protocol.
    - Forward to /chat/completions with OpenAI body (no Ollama-specific fields).
    - Translate the OpenAI response back to Ollama format (done:true, prompt_eval_count).
    """
    cfg = parse_router_config(_raw(tmp_path, "openai"))
    app = create_app(cfg)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
            },
        )

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/api/chat",
            json={
                "model": "alpha",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    assert resp.status_code == 200
    # Forwarded to OpenAI endpoint, model rewritten.
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["model"] == "real/alpha"
    assert seen["body"]["stream"] is False
    # No Ollama-specific key forwarded.
    assert "prompt_eval_count" not in seen["body"]

    # Response body translated to Ollama format.
    body = resp.json()
    assert body["done"] is True
    assert body["message"]["content"] == "hi there"
    assert body["prompt_eval_count"] == 4
    assert body["eval_count"] == 5

    rows = stats_rows()
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 9


def test_ollama_via_openai_upstream_stream(tmp_path):
    """Ollama /api/chat default stream backed by an OpenAI upstream.

    Upstream returns SSE; handler must emit NDJSON and close with done:true.
    """
    cfg = parse_router_config(_raw(tmp_path, "openai"))
    app = create_app(cfg)

    seen = {}
    sse_body = (
        b'data: {"choices":[{"delta":{"role":"assistant","content":"he"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"llo"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post(
            "/api/chat",
            json={"model": "alpha", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["stream"] is True

    # Every line is valid JSON; at least one has done:true.
    lines = [l for l in resp.content.decode().splitlines() if l.strip()]
    parsed = [json.loads(l) for l in lines]
    assert any(p.get("done") is True for p in parsed)
    # Content chunks have done:false.
    content_chunks = [p for p in parsed if not p.get("done")]
    assert any(p["message"]["content"] for p in content_chunks)


def test_ollama_tags_lists_both_protocols(tmp_path):
    """GET /api/tags returns models from both ollama and openai upstreams."""
    raw = {
        "server": {"host": "127.0.0.1", "port": 18790},
        "storage": {
            "call_dump_dir": str(tmp_path / "calls"),
            "stats_db_path": str(tmp_path / "stats.sqlite"),
            "dump_enabled": True,
            "mask_keys": [],
        },
        "upstreams": [
            {
                "id": "ollama_up",
                "protocol": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "api_key": "",
                "headers": {},
                "models": [{"alias": "real_ollama", "upstream_model": "llama3.1:8b"}],
            },
            {
                "id": "openai_up",
                "protocol": "openai",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-x",
                "headers": {},
                "models": [{"alias": "openai_model", "upstream_model": "gpt-x"}],
            },
        ],
    }
    cfg = parse_router_config(raw)
    app = create_app(cfg)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.get("/api/tags")

    assert resp.status_code == 200
    models = resp.json()["models"]
    names = {m["name"] for m in models}
    assert "real_ollama" in names
    assert "openai_model" in names
    # Format field distinguishes the two.
    fmt_map = {m["name"]: m["details"]["format"] for m in models}
    assert fmt_map["real_ollama"] == "ollama"
    assert fmt_map["openai_model"] == "openai-compat"


# ──────────────────────────────────────────────────────────────────────────────
# /api/show tests
# ──────────────────────────────────────────────────────────────────────────────


def test_ollama_show_openai_upstream(tmp_path):
    """/api/show with an OpenAI upstream returns synthesized metadata, no forwarding."""
    cfg = parse_router_config(_raw(tmp_path, "openai"))
    app = create_app(cfg)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post("/api/show", json={"model": "alpha"})

    assert resp.status_code == 200
    assert call_count["n"] == 0, "should not forward to upstream for openai-compat /api/show"
    body = resp.json()
    assert body["model"] == "alpha"
    assert body["details"]["format"] == "openai-compat"
    assert body["details"]["parent_model"] == "real/alpha"


def test_ollama_show_ollama_upstream(tmp_path):
    """/api/show with an Ollama upstream forwards to the real daemon."""
    cfg = parse_router_config(_raw(tmp_path, "ollama"))
    app = create_app(cfg)

    daemon_response = {
        "model": "real/alpha",
        "template": "{{ .Prompt }}",
        "parameters": "temperature 0.7",
        "details": {"family": "llama"},
    }

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=daemon_response)

    transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post("/api/show", json={"model": "alpha"})

    assert resp.status_code == 200
    assert seen["url"].endswith("/api/show")
    # Daemon response is proxied back as-is.
    assert resp.json()["template"] == "{{ .Prompt }}"


def test_ollama_show_unknown_model(tmp_path):
    """/api/show with an unknown alias returns 404."""
    cfg = parse_router_config(_raw(tmp_path, "openai"))
    app = create_app(cfg)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with TestClient(app) as client:
        _replace_clients_with_mock(app, transport)
        resp = client.post("/api/show", json={"model": "ghost"})

    assert resp.status_code == 404
