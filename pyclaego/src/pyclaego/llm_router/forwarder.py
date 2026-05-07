"""Outbound HTTP forwarder.

One `httpx.AsyncClient` per upstream id (lifespan-managed). Provides
unary (full-body) and streaming (raw bytes) forwarding.

The forwarder is intentionally protocol-agnostic: handlers prepare the
final URL, headers, and body; this class only performs the HTTP call.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .config import RouterConfig, UpstreamConfig

logger = logging.getLogger(__name__)

# Hop-by-hop request headers that must NOT be forwarded upstream.
_DROP_REQUEST_HEADERS = frozenset(
    h.lower()
    for h in (
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        # Auth headers are always re-set by the handler.
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        # Compression — let httpx negotiate fresh.
        "accept-encoding",
    )
)

# Hop-by-hop response headers that must NOT be relayed back.
_DROP_RESPONSE_HEADERS = frozenset(
    h.lower()
    for h in (
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
    )
)


def filter_request_headers(src: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in src.items() if k.lower() not in _DROP_REQUEST_HEADERS}


def filter_response_headers(src: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in src.items() if k.lower() not in _DROP_RESPONSE_HEADERS}


class OutboundForwarder:
    def __init__(self, config: RouterConfig) -> None:
        self._config = config
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def startup(self) -> None:
        for up in self._config.upstreams:
            self._clients[up.id] = httpx.AsyncClient(
                base_url=up.base_url,
                http2=True,
                timeout=httpx.Timeout(60.0, connect=10.0, read=600.0),
            )

    async def shutdown(self) -> None:
        for c in self._clients.values():
            try:
                await c.aclose()
            except Exception:
                logger.exception("error closing httpx client")
        self._clients.clear()

    def client_for(self, upstream: UpstreamConfig) -> httpx.AsyncClient:
        client = self._clients.get(upstream.id)
        if client is None:
            raise RuntimeError(
                f"forwarder not started or unknown upstream '{upstream.id}'"
            )
        return client

    async def forward_unary(
        self,
        *,
        upstream: UpstreamConfig,
        method: str,
        path: str,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        client = self.client_for(upstream)
        return await client.request(
            method,
            path,
            headers=dict(headers),
            params=dict(params) if params else None,
            content=content,
        )

    @asynccontextmanager
    async def forward_stream(
        self,
        *,
        upstream: UpstreamConfig,
        method: str,
        path: str,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
    ) -> AsyncIterator[httpx.Response]:
        client = self.client_for(upstream)
        req = client.build_request(
            method,
            path,
            headers=dict(headers),
            params=dict(params) if params else None,
            content=content,
        )
        resp = await client.send(req, stream=True)
        try:
            yield resp
        finally:
            await resp.aclose()

    async def start_stream(
        self,
        *,
        upstream: UpstreamConfig,
        method: str,
        path: str,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Send a streaming request and return the open response.

        The caller MUST call ``await resp.aclose()`` when done with the response.
        Unlike ``forward_stream``, this does not use a context manager so the
        caller can inspect ``resp.status_code`` before deciding whether to
        stream or to read the error body non-streaming.
        """
        client = self.client_for(upstream)
        req = client.build_request(
            method,
            path,
            headers=dict(headers),
            params=dict(params) if params else None,
            content=content,
        )
        return await client.send(req, stream=True)
