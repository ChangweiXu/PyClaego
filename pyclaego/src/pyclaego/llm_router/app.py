"""FastAPI app factory for the LLM Router proxy.

Lifespan opens per-upstream `httpx.AsyncClient` instances and the SQLite
stats store; both are closed at shutdown.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

from ..logging import get_running_log
from .config import RouterConfig, load_router_config
from .routing import RouteTable

_rlog = get_running_log()

# Paths excluded from inbound logging (health/meta, no LLM payload).
_SKIP_LOG_PREFIXES = ("/healthz", "/_router")


class _InboundLogger(BaseHTTPMiddleware):
    """Append every inbound API request (path + body) to inbound_dir/YYYYMMDD.ndjson."""

    def __init__(self, app, inbound_dir: Path) -> None:
        super().__init__(app)
        self._dir = inbound_dir

    async def dispatch(self, request: StarletteRequest, call_next) -> StarletteResponse:
        path = request.url.path
        if not any(path.startswith(p) for p in _SKIP_LOG_PREFIXES):
            # Calling request.body() here caches the raw bytes in request._body
            # so downstream handlers can still call request.body()/request.json().
            raw = await request.body()
            asyncio.create_task(self._write(request.method, path, raw))
        return await call_next(request)

    async def _write(self, method: str, path: str, raw: bytes) -> None:
        try:
            body_obj = json.loads(raw) if raw else None
        except Exception:
            body_obj = raw.decode("utf-8", errors="replace")

        if isinstance(body_obj, dict):
            # Don't log large body items.
            for k in ["messages", "tools"]:
                if k in body_obj:
                    body_obj[k] = f"<{len(body_obj[k])} items>"
            for k in ["system"]:
                if k in body_obj:
                    body_obj[k] = f"<{len(body_obj[k])} chars>"

        now = datetime.now(timezone.utc)
        line = (
            json.dumps(
                {"ts": now.isoformat(), "method": method, "path": path, "body": body_obj},
                ensure_ascii=False,
            )
            + "\n"
        )
        log_path = self._dir / f"{now.strftime('%Y%m%d')}.ndjson"
        await asyncio.get_event_loop().run_in_executor(None, _append_line, log_path, line)


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def create_app(config: RouterConfig | None = None) -> FastAPI:
    """Build a FastAPI app for the given router config (or load default)."""
    cfg = config or load_router_config()
    route_table = RouteTable(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Imported lazily to avoid a hard dependency on these modules during
        # the early skeleton phase.
        from .forwarder import OutboundForwarder
        from .recording.call_dumper import CallDumper
        from .recording.stats_store import StatsStore

        forwarder = OutboundForwarder(cfg)
        await forwarder.startup()
        dumper = CallDumper(cfg.storage)
        await dumper.startup()
        stats = StatsStore(cfg.storage.stats_db_path)
        await stats.startup()

        app.state.config = cfg
        app.state.routes = route_table
        app.state.forwarder = forwarder
        app.state.dumper = dumper
        app.state.stats = stats

        _rlog.info(
            "llm_router",
            f"llm_router started: {len(cfg.upstreams)} upstreams, "
            f"{sum(len(u.models) for u in cfg.upstreams)} total aliases",
        )
        try:
            yield
        finally:
            await forwarder.shutdown()
            await stats.shutdown()
            _rlog.info("llm_router", "llm_router stopped")

    app = FastAPI(title="pyclaego LLM Router", lifespan=lifespan)

    inbound_dir = cfg.storage.call_dump_dir / "inbounds"
    app.add_middleware(_InboundLogger, inbound_dir=inbound_dir)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "upstreams": [u.id for u in cfg.upstreams],
            "aliases": [
                {"protocol": r.protocol, "alias": r.alias, "upstream": r.upstream.id}
                for r in route_table.list_aliases()
            ],
        }

    @app.get("/_router/models")
    async def router_models() -> dict:
        return {
            "data": [
                {
                    "protocol": r.protocol,
                    "alias": r.alias,
                    "upstream": r.upstream.id,
                    "upstream_model": r.upstream_model,
                }
                for r in route_table.list_aliases()
            ]
        }

    # Inbound routers are registered here once Phase C lands.
    from .inbound.anthropic_routes import router as anthropic_router
    from .inbound.gemini_routes import router as gemini_router
    from .inbound.ollama_routes import router as ollama_router
    from .inbound.openai_routes import router as openai_router

    app.include_router(openai_router)
    app.include_router(anthropic_router)
    app.include_router(gemini_router)
    app.include_router(ollama_router)

    return app
