"""Network-egress filter rule — SSRF / metadata endpoint / private-network guard.

For web_fetch / web_search / download_file style tools, checks whether the
target host matches a denylist / suffix list / private CIDR.

Example config:
```yaml
rule_type: "network_egress"
rule_id: "network_egress_filter"
enabled: false
request_types: ["tool_call"]
action: "deny"
tool_names:
  - "web_fetch"
  - "web_fetch_v2"
  - "web_search"
  - "download_file"
block_private_networks: true       # shortcut: block RFC1918 / loopback / link-local
denied_hosts:
  - "169.254.169.254"              # AWS/GCP metadata
  - ".internal"
  - ".local"
denied_cidrs:
  - "10.0.0.0/8"
  - "172.16.0.0/12"
  - "192.168.0.0/16"
  - "127.0.0.0/8"
  - "169.254.0.0/16"
  - "::1/128"
  - "fc00::/7"
  - "fe80::/10"
allowed_hosts: []                  # non-empty -> strict allowlist mode
```
"""

import ipaddress
import socket
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

from ..base_rule import BaseSecurityRule
from ...logging import get_running_log

_rlog = get_running_log()

_PRIVATE_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

_URL_ARG_NAMES = ("url", "urls", "target", "uri", "query_url")


class NetworkEgressRule(BaseSecurityRule):
    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.block_private_networks: bool = bool(rule_config.get("block_private_networks", True))
        self.denied_hosts: List[str] = list(rule_config.get("denied_hosts", []))
        self.allowed_hosts: List[str] = list(rule_config.get("allowed_hosts", []))

        cidrs = list(rule_config.get("denied_cidrs", []))
        if self.block_private_networks:
            for c in _PRIVATE_CIDRS:
                if c not in cidrs:
                    cidrs.append(c)
        self._denied_networks = []
        for c in cidrs:
            try:
                self._denied_networks.append(ipaddress.ip_network(c, strict=False))
            except ValueError as e:
                _rlog.warning("core_service", f"[NetworkEgress] Invalid CIDR {c}: {e}")

        self._last_reason: str = ""

    async def matches(self, request: Dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False
        tool_name = request.get("tool_name", "")
        if not self.applies_to_tool(tool_name):
            return False

        urls = list(self._extract_urls(request.get("tool_args") or {}))
        if not urls:
            return False

        for url in urls:
            host = self._extract_host(url)
            if not host:
                continue
            verdict, reason = self._check_host(host)
            if verdict:
                self._last_reason = f"blocked url='{url}' host='{host}': {reason}"
                _rlog.warning("core_service", f"[NetworkEgress] {self._last_reason}")
                return True
        return False

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()

    # ── helpers ──

    def _extract_urls(self, args: Any) -> Iterable[str]:
        if isinstance(args, dict):
            for k, v in args.items():
                if k in _URL_ARG_NAMES:
                    if isinstance(v, str):
                        yield v
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                yield item
                else:
                    yield from self._extract_urls(v)
        elif isinstance(args, list):
            for v in args:
                yield from self._extract_urls(v)

    @staticmethod
    def _extract_host(url: str) -> str:
        try:
            parsed = urlparse(url if "://" in url else f"http://{url}")
            return (parsed.hostname or "").lower()
        except Exception:
            return ""

    def _check_host(self, host: str) -> tuple:
        # Allowlist mode
        if self.allowed_hosts:
            if not self._host_in_list(host, self.allowed_hosts):
                return True, "host not in allowed_hosts"

        # Denied hosts (exact / suffix)
        if self._host_in_list(host, self.denied_hosts):
            return True, "host in denied_hosts"

        # Denied CIDR
        ips = self._resolve(host)
        for ip in ips:
            for net in self._denied_networks:
                try:
                    if ip in net:
                        return True, f"ip {ip} in denied network {net}"
                except TypeError:
                    continue
        return False, ""

    @staticmethod
    def _host_in_list(host: str, patterns: List[str]) -> bool:
        host = host.lower()
        for p in patterns:
            p = p.lower()
            if p.startswith("."):
                if host.endswith(p) or host == p[1:]:
                    return True
            elif host == p:
                return True
        return False

    @staticmethod
    def _resolve(host: str) -> List[Any]:
        # If host is already an IP literal, return it directly
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None)
            ips = []
            for info in infos:
                addr = info[4][0]
                try:
                    ips.append(ipaddress.ip_address(addr))
                except ValueError:
                    continue
            return ips
        except Exception:
            return []
