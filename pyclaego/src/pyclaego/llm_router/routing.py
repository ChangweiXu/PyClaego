"""Route table: (protocol, alias) -> ResolvedRoute."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RouterConfig, UpstreamConfig


@dataclass(frozen=True)
class ResolvedRoute:
    upstream: UpstreamConfig
    alias: str
    upstream_model: str

    @property
    def protocol(self) -> str:
        return self.upstream.protocol


class RouteTable:
    """Indexed lookup of routes built from a RouterConfig."""

    def __init__(self, config: RouterConfig) -> None:
        self._table: dict[tuple[str, str], ResolvedRoute] = {}
        for up in config.upstreams:
            for m in up.models:
                self._table[(up.protocol, m.alias)] = ResolvedRoute(
                    upstream=up, alias=m.alias, upstream_model=m.upstream_model
                )

    def resolve(self, protocol: str, alias: str) -> ResolvedRoute | None:
        return self._table.get((protocol, alias))

    def list_aliases(self, protocol: str | None = None) -> list[ResolvedRoute]:
        if protocol is None:
            return list(self._table.values())
        return [r for (p, _), r in self._table.items() if p == protocol]
