"""Shared handler base.

Provides:
- Resolution helper from (protocol, alias) -> ResolvedRoute
- Recording helper that builds + persists a CallRecord
- A small set of utility methods for building outbound headers/params
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request

from ...logging import get_running_log
from ..forwarder import OutboundForwarder, filter_request_headers
from ..recording.call_dumper import CallDumper, CallRecord
from ..recording.stats_store import StatsStore
from ..routing import ResolvedRoute, RouteTable

_rlog = get_running_log()


class HandlerContext:
    """Bundle of per-request services pulled from app.state."""

    def __init__(self, request: Request) -> None:
        st = request.app.state
        self.routes: RouteTable = st.routes
        self.forwarder: OutboundForwarder = st.forwarder
        self.dumper: CallDumper = st.dumper
        self.stats: StatsStore = st.stats


def resolve_or_404(
    routes: RouteTable, protocol: str, alias: str
) -> ResolvedRoute:
    route = routes.resolve(protocol, alias)
    if route is None:
        _rlog.warning(
            "llm_router",
            f"[{protocol}] 404 no route for alias '{alias}'",
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "model_not_found",
                    "message": (
                        f"no route for model '{alias}' under protocol '{protocol}'"
                    ),
                }
            },
        )
    return route


async def record_call(
    *,
    ctx: HandlerContext,
    route: ResolvedRoute,
    method: str,
    url: str,
    request_headers: Mapping[str, str],
    request_params: Mapping[str, Any] | None,
    request_body: Any,
    response_status: int | None,
    response_headers: Mapping[str, str] | None,
    response_body: Any,
    started_at: float,
    first_byte_at: float | None,
    finished_at: float,
    stream: bool,
    usage: tuple,
    error: str | None,
    merged_body: dict[str, Any] | None = None,
) -> None:
    p, c, t = usage
    latency_ms = int((finished_at - started_at) * 1000)
    ttft_ms: int | None = (
        int((first_byte_at - started_at) * 1000) if first_byte_at else None
    )
    record = CallRecord(
        protocol=route.protocol,
        alias=route.alias,
        upstream_id=route.upstream.id,
        upstream_model=route.upstream_model,
        request={
            "method": method,
            "url": url,
            "headers": dict(request_headers),
            "params": dict(request_params) if request_params else None,
            "body": request_body,
        },
        response={
            "status": response_status,
            "headers": dict(response_headers) if response_headers else None,
            "body": response_body,
        },
        timing={
            "started_at": started_at,
            "first_byte_at": first_byte_at,
            "finished_at": finished_at,
            "latency_ms": latency_ms,
            "ttft_ms": ttft_ms,
        },
        usage={"prompt_tokens": p, "completion_tokens": c, "total_tokens": t},
        stream=stream,
        error=error,
        merged_body=merged_body,
    )
    dump_path = await ctx.dumper.write(record)
    await ctx.stats.insert(
        protocol=route.protocol,
        alias=route.alias,
        upstream_id=route.upstream.id,
        upstream_model=route.upstream_model,
        status=response_status,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        prompt_tokens=p,
        completion_tokens=c,
        total_tokens=t,
        stream=stream,
        error=error,
        dump_path=str(dump_path) if dump_path else None,
    )


def merge_outbound_headers(
    inbound: Mapping[str, str],
    upstream_headers: Mapping[str, str],
    auth_headers: Mapping[str, str],
) -> dict[str, str]:
    """Build outbound headers: filtered inbound, plus upstream-fixed, plus auth."""
    out = filter_request_headers(inbound)
    for k, v in upstream_headers.items():
        out[k] = v
    for k, v in auth_headers.items():
        out[k] = v
    return out


def now() -> float:
    return time.time()
