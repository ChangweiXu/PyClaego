"""Cost-budget rule.

Accumulates LLM token usage per session and blocks further requests once
the budget is exceeded.

Requires ``SecurityMonitor.after_llm_call`` to forward ``usage`` inside the
``result`` dict.

Example config:
```yaml
rule_type: "cost_budget"
rule_id: "session_cost_budget"
enabled: false
request_types: ["llm_call"]
action: "deny"
max_total_tokens: 500000           # 0 disables the check
max_input_tokens: 0
max_output_tokens: 0
max_usd: 0.0                       # 0 disables the check
pricing:                           # optional: provider_id -> {input_per_1k, output_per_1k}
  kimi_code:
    input_per_1k: 0.0015
    output_per_1k: 0.003
```
"""

from collections import defaultdict
from typing import Any, Dict, Optional

from ..base_rule import BaseSecurityRule
from ...logging import get_running_log

_rlog = get_running_log()


class CostBudgetRule(BaseSecurityRule):
    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.max_total_tokens = int(rule_config.get("max_total_tokens", 0))
        self.max_input_tokens = int(rule_config.get("max_input_tokens", 0))
        self.max_output_tokens = int(rule_config.get("max_output_tokens", 0))
        self.max_usd = float(rule_config.get("max_usd", 0.0))
        self.pricing: Dict[str, Dict[str, float]] = rule_config.get("pricing", {}) or {}

        # session_id -> counters
        self._counters: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"input": 0, "output": 0, "total": 0, "usd": 0.0}
        )
        self._last_reason: str = ""

    async def matches(self, request: Dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False

        session_id = request.get("session_id", "default")
        c = self._counters[session_id]

        if self.max_total_tokens and c["total"] >= self.max_total_tokens:
            self._last_reason = f"total tokens {int(c['total'])} >= budget {self.max_total_tokens}"
            return self._deny_with_log()
        if self.max_input_tokens and c["input"] >= self.max_input_tokens:
            self._last_reason = f"input tokens {int(c['input'])} >= budget {self.max_input_tokens}"
            return self._deny_with_log()
        if self.max_output_tokens and c["output"] >= self.max_output_tokens:
            self._last_reason = f"output tokens {int(c['output'])} >= budget {self.max_output_tokens}"
            return self._deny_with_log()
        if self.max_usd and c["usd"] >= self.max_usd:
            self._last_reason = f"cost ${c['usd']:.4f} >= budget ${self.max_usd:.4f}"
            return self._deny_with_log()
        return False

    async def on_request_completed(
        self,
        request: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not result:
            return
        # Only accumulate in the after_llm_call phase
        if result.get("hook") != "after_llm_call":
            return
        usage = result.get("usage") or {}
        if not usage:
            return

        session_id = request.get("session_id", "default")
        llm_id = request.get("llm_id", "")

        input_tokens = int(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or 0
        )
        output_tokens = int(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or 0
        )
        total_tokens = int(
            usage.get("total_tokens")
            or (input_tokens + output_tokens)
        )

        c = self._counters[session_id]
        c["input"] += input_tokens
        c["output"] += output_tokens
        c["total"] += total_tokens

        price = self.pricing.get(llm_id)
        if price:
            cost = (
                input_tokens / 1000.0 * float(price.get("input_per_1k", 0))
                + output_tokens / 1000.0 * float(price.get("output_per_1k", 0))
            )
            c["usd"] += cost

    def get_match_reason(self) -> str:
        return self._last_reason or super().get_match_reason()

    def _deny_with_log(self) -> bool:
        _rlog.warning("core_service", f"[CostBudget] {self._last_reason}")
        return True
