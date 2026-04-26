"""Subagent recursion-depth / total-count limit rule.

Blocks ``spawn_subagent`` tool calls that would exceed the configured depth
or the total number of subagents within a root session.

Depth is not encoded in ``subagent_id`` directly; we infer it from whether
``request.subagent_id`` is non-empty (plus an explicit override via
``tool_args._depth`` when available). A non-empty ``subagent_id`` implies
``depth >= 1``.
"""

from collections import defaultdict
from typing import Any, Dict, Optional

from ..base_rule import BaseSecurityRule
from ...logging import get_running_log

_rlog = get_running_log()


class SubagentDepthRule(BaseSecurityRule):
    _SPAWN_TOOL_NAMES = frozenset(["spawn_subagent"])

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.max_depth: int = int(rule_config.get("max_depth", 3))
        self.max_total: int = int(rule_config.get("max_total_subagents_per_session", 20))

        # session_id -> total spawned
        self._totals: Dict[str, int] = defaultdict(int)
        self._last_reason: str = ""

    async def matches(self, request: Dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False
        if request.get("tool_name", "") not in self._SPAWN_TOOL_NAMES:
            return False

        session_id = request.get("session_id", "default")
        subagent_id = request.get("subagent_id")

        # depth: explicit via tool_args._depth, else assume depth>=1 for
        # subagent-initiated spawns, 0 otherwise.
        tool_args = request.get("tool_args") or {}
        explicit_depth = tool_args.get("_depth")
        if explicit_depth is not None:
            try:
                parent_depth = int(explicit_depth)
            except (TypeError, ValueError):
                parent_depth = 1 if subagent_id else 0
        else:
            parent_depth = 1 if subagent_id else 0

        next_depth = parent_depth + 1
        if next_depth > self.max_depth:
            self._last_reason = (
                f"subagent depth {next_depth} > max_depth {self.max_depth}"
            )
            _rlog.warning("core_service", f"[SubagentDepth] {self._last_reason}")
            return True

        if self.max_total and self._totals[session_id] + 1 > self.max_total:
            self._last_reason = (
                f"session total subagents {self._totals[session_id] + 1} "
                f"> max_total {self.max_total}"
            )
            _rlog.warning("core_service", f"[SubagentDepth] {self._last_reason}")
            return True

        return False

    async def on_request_completed(
        self,
        request: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not result or result.get("hook") != "before":
            return
        if request.get("tool_name") not in self._SPAWN_TOOL_NAMES:
            return
        # Only count when the request was not denied
        if result.get("decision") == "deny":
            return
        session_id = request.get("session_id", "default")
        self._totals[session_id] += 1

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()
