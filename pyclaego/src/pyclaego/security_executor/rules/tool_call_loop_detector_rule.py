"""Tool-call loop detection rule.

Detects consecutive repeated invocations of the same (tool_name, args) within
a session — the classic "agent stuck calling the same failing tool" pattern.
No LLM overhead.

Example config:
```yaml
rule_type: "tool_call_loop_detector"
rule_id: "tool_loop_detector"
enabled: false
request_types: ["tool_call"]
action: "warn"
threshold: 3                      # consecutive-repeat threshold
window: 10                        # number of recent calls to retain
```
"""

import hashlib
import json
from collections import defaultdict, deque
from typing import Any

from ...logging import get_running_log
from ..base_rule import BaseSecurityRule

_rlog = get_running_log()


class ToolCallLoopDetectorRule(BaseSecurityRule):
    """Detect consecutive repeated (tool_name, args) calls within a session."""

    def __init__(self, rule_config: dict[str, Any]):
        super().__init__(rule_config)
        self.threshold: int = int(rule_config.get("threshold", 3))
        self.window: int = int(rule_config.get("window", 10))

        # session_id -> deque[(tool_name, args_hash)]
        self._history: dict[str, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.window)
        )
        self._last_reason: str = ""

    async def matches(self, request: dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False

        tool_name = request.get("tool_name", "")
        if not tool_name:
            return False

        session_id = request.get("session_id", "default")
        args_hash = self._hash_args(request.get("tool_args", {}))
        history = self._history[session_id]

        consecutive = 1
        for name, ah in reversed(history):
            if name == tool_name and ah == args_hash:
                consecutive += 1
            else:
                break

        # Count including the current (not-yet-enqueued) request; trigger on
        # ``consecutive >= threshold``.
        if consecutive >= self.threshold:
            self._last_reason = (
                f"tool '{tool_name}' has been called {consecutive} times consecutively "
                f"with identical args (threshold={self.threshold})"
            )
            _rlog.warning("core_service", f"[ToolCallLoopDetector] {self._last_reason}")
            return True
        return False

    async def on_request_completed(
        self,
        request: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        # Only enqueue in the before phase to avoid double-counting
        if not result or result.get("hook") != "before":
            return
        if not self.applies_to_request_type(request.get("type", "")):
            return
        tool_name = request.get("tool_name", "")
        if not tool_name:
            return
        session_id = request.get("session_id", "default")
        args_hash = self._hash_args(request.get("tool_args", {}))
        self._history[session_id].append((tool_name, args_hash))

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()

    @staticmethod
    def _hash_args(args: Any) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            payload = str(args)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()
