"""File-size limit rule.

- read_file: reject reads of files larger than ``max_read_bytes``.
- write_file / file_edit: reject writes whose ``content`` exceeds
  ``max_write_bytes``.

Example config:
```yaml
rule_type: "file_size"
rule_id: "file_size_limit"
enabled: false
request_types: ["tool_call"]
action: "deny"
max_read_bytes: 5242880      # 5 MiB
max_write_bytes: 2097152     # 2 MiB
```
"""

import os
from typing import Any

from ...logging import get_running_log
from ..base_rule import BaseSecurityRule

_rlog = get_running_log()

_READ_TOOLS = frozenset(["read_file", "read_pdf", "read_image_base64"])
_WRITE_TOOLS = frozenset(["write_file", "file_edit"])


class FileSizeRule(BaseSecurityRule):
    def __init__(self, rule_config: dict[str, Any]):
        super().__init__(rule_config)
        self.max_read_bytes = int(rule_config.get("max_read_bytes", 0))
        self.max_write_bytes = int(rule_config.get("max_write_bytes", 0))
        self._last_reason: str = ""

    async def matches(self, request: dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False

        tool_name = request.get("tool_name", "")
        tool_args = request.get("tool_args") or {}

        if self.max_read_bytes and tool_name in _READ_TOOLS:
            path = self._pick_path(tool_args)
            if path and os.path.isfile(path):
                try:
                    size = os.path.getsize(path)
                except OSError:
                    return False
                if size > self.max_read_bytes:
                    self._last_reason = (
                        f"read size {size} > max_read_bytes {self.max_read_bytes} "
                        f"(path={path})"
                    )
                    _rlog.warning("core_service", f"[FileSize] {self._last_reason}")
                    return True

        if self.max_write_bytes and tool_name in _WRITE_TOOLS:
            content = tool_args.get("content")
            if isinstance(content, (str, bytes)):
                size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
                if size > self.max_write_bytes:
                    self._last_reason = (
                        f"write size {size} > max_write_bytes {self.max_write_bytes}"
                    )
                    _rlog.warning("core_service", f"[FileSize] {self._last_reason}")
                    return True

        return False

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()

    @staticmethod
    def _pick_path(args: dict[str, Any]) -> str:
        for key in ("path", "file_path", "filepath", "filename"):
            v = args.get(key)
            if isinstance(v, str) and v:
                return os.path.expanduser(v)
        return ""
