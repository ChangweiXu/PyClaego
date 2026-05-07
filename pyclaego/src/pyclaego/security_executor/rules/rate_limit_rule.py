"""Rate-limit rule (sliding window).

Limits per-session tool_call / llm_call frequency on minute and hour scales.

Example config:
```yaml
rule_type: "rate_limit"
rule_id: "per_session_rate_limit"
enabled: false
request_types: ["tool_call", "llm_call"]
action: "deny"
tool_calls_per_min: 60
llm_calls_per_min: 30
tool_calls_per_hour: 600
llm_calls_per_hour: 300
```
"""

import time
from collections import defaultdict, deque
from typing import Any

from ...logging import get_running_log
from ..base_rule import BaseSecurityRule

_rlog = get_running_log()


class RateLimitRule(BaseSecurityRule):
    _MIN = 60.0
    _HOUR = 3600.0

    def __init__(self, rule_config: dict[str, Any]):
        super().__init__(rule_config)
        self.tool_per_min = int(rule_config.get("tool_calls_per_min", 0))
        self.llm_per_min = int(rule_config.get("llm_calls_per_min", 0))
        self.tool_per_hour = int(rule_config.get("tool_calls_per_hour", 0))
        self.llm_per_hour = int(rule_config.get("llm_calls_per_hour", 0))

        # (session_id, type) -> deque[timestamp]
        self._windows: dict[tuple, deque[float]] = defaultdict(deque)
        self._last_reason: str = ""

    async def matches(self, request: dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        req_type = request.get("type", "")
        if not self.applies_to_request_type(req_type):
            return False

        session_id = request.get("session_id", "default")
        now = time.time()
        key = (session_id, req_type)
        window = self._windows[key]
        self._trim(window, now, self._HOUR)  # keep last hour

        # Count calls in the last minute / last hour
        count_min = sum(1 for t in window if now - t <= self._MIN)
        count_hour = len(window)

        limit_min = self.tool_per_min if req_type == "tool_call" else self.llm_per_min
        limit_hour = self.tool_per_hour if req_type == "tool_call" else self.llm_per_hour

        if limit_min and count_min + 1 > limit_min:
            self._last_reason = (
                f"{req_type} rate exceeded: {count_min + 1}/{limit_min} per minute"
            )
            _rlog.warning("core_service", f"[RateLimit] {self._last_reason}")
            return True
        if limit_hour and count_hour + 1 > limit_hour:
            self._last_reason = (
                f"{req_type} rate exceeded: {count_hour + 1}/{limit_hour} per hour"
            )
            _rlog.warning("core_service", f"[RateLimit] {self._last_reason}")
            return True
        return False

    async def on_request_completed(
        self,
        request: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        if not result or result.get("hook") != "before":
            return
        req_type = request.get("type", "")
        if not self.applies_to_request_type(req_type):
            return
        session_id = request.get("session_id", "default")
        key = (session_id, req_type)
        self._windows[key].append(time.time())

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()

    @staticmethod
    def _trim(window: deque[float], now: float, horizon: float) -> None:
        while window and now - window[0] > horizon:
            window.popleft()
