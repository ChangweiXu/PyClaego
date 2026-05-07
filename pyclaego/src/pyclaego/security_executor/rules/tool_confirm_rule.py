"""tool_confirm_rule — pause-and-confirm rule for risky tool calls.

A rule of this type fires when ``tool_name`` is in the configured
``tool_names`` list and returns ``SecurityDecision.QUERY``.  The
``query_spec`` section controls what the user sees.

Example config::

    security:
      rules:
        - rule_id: confirm_bash
          rule_type: tool_confirm
          enabled: true
          tool_names: [bash]
          query_spec:
            prompt: "Allow running this bash command?\\n```\\n{tool_args.command}\\n```"
            choices:
              - {value: allow, label: "Yes, run it"}
              - {value: deny,  label: "No, cancel"}
            deny_values: [deny]
            default: deny
            timeout_s: 300
"""

from __future__ import annotations

from typing import Any

from ...logging import get_running_log
from ..base_rule import BaseSecurityRule, SecurityDecision

_rlog = get_running_log()

_DEFAULT_QUERY_SPEC: dict[str, Any] = {
    "prompt": "Allow this tool call?",
    "choices": [
        {"value": "allow", "label": "Allow"},
        {"value": "deny",  "label": "Deny"},
    ],
    "deny_values": ["deny"],
    "default": "deny",
    "timeout_s": None,
}


class ToolConfirmRule(BaseSecurityRule):
    """Pause execution and ask the user to confirm before running the tool.

    Matching condition: ``tool_name`` is in ``self.tool_names`` (if empty,
    matches all tool calls).

    Decision: always ``QUERY`` (action field is forced).
    """

    def __init__(self, rule_config: dict[str, Any]) -> None:
        # Force action to "query" regardless of what is in config
        rule_config = dict(rule_config)
        rule_config["action"] = "query"
        super().__init__(rule_config)
        # Merge defaults with user-provided spec
        raw_spec = rule_config.get("query_spec") or {}
        self._query_spec: dict[str, Any] = {**_DEFAULT_QUERY_SPEC, **raw_spec}
        self._last_reason: str = f"tool_confirm rule {self.rule_id}"

    async def matches(self, request: dict[str, Any]) -> bool:
        """Match tool calls whose name is in ``self.tool_names``."""
        if not self.enabled:
            return False
        if request.get("type") != "tool_call":
            return False
        tool_name: str | None = request.get("tool_name")
        if not self.applies_to_tool(tool_name):
            return False
        self._last_reason = (
            f"tool '{tool_name}' requires user confirmation (rule: {self.rule_id})"
        )
        return True

    def get_match_reason(self) -> str:
        return self._last_reason

    def get_decision(self) -> SecurityDecision:
        return SecurityDecision.QUERY

    def get_query_spec(self) -> dict[str, Any]:
        return self._query_spec
