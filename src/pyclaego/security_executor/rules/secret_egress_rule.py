"""Secret / credential egress-filter rule.

Scans tool arguments for common credential / secret patterns to prevent
accidental leakage through web_fetch / download_file / write_file style
tools.

Example config:
```yaml
rule_type: "secret_egress"
rule_id: "secret_egress_scan"
enabled: false
request_types: ["tool_call"]
action: "deny"
tool_names:
  - "web_fetch"
  - "web_fetch_v2"
  - "download_file"
  - "write_file"
  - "file_edit"
extra_patterns:
  - "api_key\\s*=\\s*[A-Za-z0-9]{20,}"
```
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from ..base_rule import BaseSecurityRule
from ...logging import get_running_log

_rlog = get_running_log()


# (name, regex)
_DEFAULT_PATTERNS: List[Tuple[str, str]] = [
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("aws_secret_key", r"(?i)aws(.{0,20})?(secret|private)[^a-z0-9]{0,5}[A-Za-z0-9/+=]{40}"),
    ("google_api_key", r"AIza[0-9A-Za-z\-_]{35}"),
    ("slack_token", r"xox[abpr]-[0-9A-Za-z\-]{10,48}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("jwt", r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    ("pem_private_key", r"-----BEGIN (?:RSA|EC|OPENSSH|DSA|PGP)? ?PRIVATE KEY-----"),
    ("generic_secret", r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
]


class SecretEgressRule(BaseSecurityRule):
    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        patterns = list(_DEFAULT_PATTERNS)
        for extra in rule_config.get("extra_patterns", []) or []:
            patterns.append(("custom", str(extra)))
        self._patterns = [(name, re.compile(p)) for name, p in patterns]
        self._last_reason: str = ""

    async def matches(self, request: Dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False
        if not self.applies_to_tool(request.get("tool_name", "")):
            return False

        payload = self._flatten(request.get("tool_args") or {})
        if not payload:
            return False

        for name, regex in self._patterns:
            m = regex.search(payload)
            if m:
                redacted = self._redact(m.group(0))
                self._last_reason = f"secret pattern '{name}' matched: {redacted}"
                _rlog.warning("core_service", f"[SecretEgress] {self._last_reason}")
                return True
        return False

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()

    @staticmethod
    def _flatten(obj: Any, buf: Optional[List[str]] = None) -> str:
        if buf is None:
            buf = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                buf.append(str(k))
                SecretEgressRule._flatten(v, buf)
        elif isinstance(obj, list):
            for v in obj:
                SecretEgressRule._flatten(v, buf)
        elif obj is not None:
            buf.append(str(obj))
        return "\n".join(buf) if buf else ""

    @staticmethod
    def _redact(s: str) -> str:
        if len(s) <= 8:
            return "***"
        return f"{s[:4]}\u2026{s[-3:]} (len={len(s)})"
