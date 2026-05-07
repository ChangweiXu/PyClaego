"""SoulV6 Phase 6 — observability primitives.

仅做"采集 + 渲染"，不做存储/聚合（聚合交给 task_manager 的子任务 metadata）。

核心数据：
- ``SoulV6TurnMetrics``：单轮内的累积计数（tokens、spill、evict、recall_stage 时延 等）
- ``SoulV6MetricsCollector``：附着到 handler 的实例；提供 ``incr/observe/snapshot/reset``
- ``classify_message_tenant``：根据 marker/role 把 ``UnifiedMessage`` 归到一个 budget tenant

设计说明：
- 不引入外部依赖；与 ``SoulV6BudgetPlan`` 的 tenant 名称对齐。
- 时间度量统一用 ``time.perf_counter()`` 的差值（毫秒级浮点）。
- 该模块**不**直接调 SessionTaskHandlerV2 —— 由调用方决定是否落子任务。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..llm import UnifiedMessage
    from .soulv6_budget_allocator import SoulV6BudgetPlan


# Tenant 名称（与 SoulV6BudgetPlan 对齐）
TENANT_SYSTEM = "system"
TENANT_TOOL_DEFS = "tool_defs"
TENANT_PREFERENCES = "preferences"
TENANT_RECALL = "recall"
TENANT_ENTITY_CARDS = "entity_cards"
TENANT_OPEN_LOOPS = "open_loops"
TENANT_HISTORY_BRIEFS = "history_briefs"
TENANT_HISTORY_VERBATIM = "history_verbatim"
TENANT_TOOL_RESULTS_LIVE = "tool_results_live"
TENANT_CURRENT_TURN = "current_turn"

ALL_TENANTS = [
    TENANT_SYSTEM, TENANT_TOOL_DEFS, TENANT_PREFERENCES,
    TENANT_RECALL, TENANT_ENTITY_CARDS, TENANT_OPEN_LOOPS,
    TENANT_HISTORY_BRIEFS, TENANT_HISTORY_VERBATIM,
    TENANT_TOOL_RESULTS_LIVE, TENANT_CURRENT_TURN,
]

# Marker → tenant
_MARKER_TO_TENANT = {
    "【更早的对话摘要": TENANT_HISTORY_BRIEFS,
    "【实体卡片": TENANT_ENTITY_CARDS,
    "【尚未闭合的问题": TENANT_OPEN_LOOPS,
    "【相关召回": TENANT_RECALL,
    "【召回的相关记忆": TENANT_RECALL,
}


def classify_message_tenant(msg: UnifiedMessage, is_last_user: bool = False) -> str:
    """把一条 UnifiedMessage 归到一个 budget tenant"""
    role = getattr(msg, "role", "")
    text = (getattr(msg, "text", "") or "")[:64]
    tool_results = getattr(msg, "tool_results", None) or []

    if role == "system":
        return TENANT_SYSTEM
    if tool_results:
        return TENANT_TOOL_RESULTS_LIVE

    if role == "user":
        for marker, tenant in _MARKER_TO_TENANT.items():
            if text.startswith(marker):
                return tenant
        if is_last_user:
            return TENANT_CURRENT_TURN
        return TENANT_HISTORY_VERBATIM

    if role == "assistant":
        return TENANT_HISTORY_VERBATIM

    return TENANT_SYSTEM


@dataclass
class SoulV6TurnMetrics:
    """单轮观测快照"""
    # 工具结果生命周期
    spill_count: int = 0
    spill_saved_tokens: int = 0
    evict_drops: int = 0
    evict_summaries: int = 0
    evict_saved_tokens: int = 0

    # 写前审查
    write_review_total: int = 0
    write_review_blocked: int = 0
    write_review_linked: int = 0

    # 召回（毫秒）
    recall_stages_ms: dict[str, float] = field(default_factory=dict)
    recall_total_ms: float = 0.0
    recall_n_results: int = 0

    # 实际用量 vs 预算（每个 tenant）
    tenant_tokens: dict[str, int] = field(default_factory=dict)
    tenant_budget: dict[str, int] = field(default_factory=dict)

    # 总账
    total_tokens: int = 0
    context_window_cap: int = 0

    def to_metadata(self) -> dict[str, Any]:
        """落盘到 SessionTaskHandlerV2 子任务 metadata 的紧凑形式"""
        # 仅保留有数值的 tenant，便于阅读
        tenant_summary = {}
        for t in ALL_TENANTS:
            used = self.tenant_tokens.get(t, 0)
            budget = self.tenant_budget.get(t, 0)
            if used == 0 and budget == 0:
                continue
            ratio = (used / budget) if budget else None
            tenant_summary[t] = {
                "used": used,
                "budget": budget,
                "ratio": round(ratio, 3) if ratio is not None else None,
            }
        return {
            "spill_count": self.spill_count,
            "spill_saved_tokens": self.spill_saved_tokens,
            "evict_drops": self.evict_drops,
            "evict_summaries": self.evict_summaries,
            "evict_saved_tokens": self.evict_saved_tokens,
            "write_review_total": self.write_review_total,
            "write_review_blocked": self.write_review_blocked,
            "write_review_linked": self.write_review_linked,
            "recall_total_ms": round(self.recall_total_ms, 2),
            "recall_n_results": self.recall_n_results,
            "recall_stages_ms": {k: round(v, 2) for k, v in self.recall_stages_ms.items()},
            "total_tokens": self.total_tokens,
            "context_window_cap": self.context_window_cap,
            "tenants": tenant_summary,
        }

    def render_human(self) -> str:
        """人类可读的简短报表（用于 /memories 或 log）"""
        lines = ["[SoulV6 metrics]"]
        if self.context_window_cap:
            pct = (self.total_tokens / self.context_window_cap * 100.0) if self.context_window_cap else 0
            lines.append(
                f"  total: {self.total_tokens}/{self.context_window_cap} ({pct:.1f}%)"
            )
        lines.append(
            f"  spill={self.spill_count} (saved≈{self.spill_saved_tokens}) "
            f"evict drops={self.evict_drops} sum={self.evict_summaries} "
            f"(saved≈{self.evict_saved_tokens})"
        )
        if self.write_review_total:
            lines.append(
                f"  write_review: total={self.write_review_total} "
                f"blocked={self.write_review_blocked} linked={self.write_review_linked}"
            )
        if self.recall_total_ms:
            stages = ", ".join(
                f"{k}={v:.0f}ms" for k, v in self.recall_stages_ms.items()
            )
            lines.append(
                f"  recall: {self.recall_total_ms:.0f}ms n={self.recall_n_results} "
                f"({stages})"
            )
        # 超预算 tenant 警告
        warnings = []
        for t in ALL_TENANTS:
            used = self.tenant_tokens.get(t, 0)
            budget = self.tenant_budget.get(t, 0)
            if budget and used > budget:
                warnings.append(f"{t}: {used}/{budget}")
        if warnings:
            lines.append("  ⚠ over-budget: " + ", ".join(warnings))
        return "\n".join(lines)


class SoulV6MetricsCollector:
    """每个 handler 实例一个；非线程安全（与单 session loop 一致）"""

    def __init__(self) -> None:
        self._current: SoulV6TurnMetrics = SoulV6TurnMetrics()
        self._timers: dict[str, float] = {}

    @property
    def current(self) -> SoulV6TurnMetrics:
        return self._current

    def reset(self) -> SoulV6TurnMetrics:
        prev = self._current
        self._current = SoulV6TurnMetrics()
        self._timers.clear()
        return prev

    # ---- counters ----
    def incr(self, field_name: str, n: int = 1) -> None:
        cur = getattr(self._current, field_name, None)
        if isinstance(cur, int):
            setattr(self._current, field_name, cur + n)

    def add_tokens(self, field_name: str, n: int) -> None:
        self.incr(field_name, max(0, int(n)))

    # ---- recall stage timers ----
    def stage_start(self, stage: str) -> None:
        self._timers[stage] = time.perf_counter()

    def stage_end(self, stage: str, n_results: int = 0) -> float:
        t0 = self._timers.pop(stage, None)
        if t0 is None:
            return 0.0
        ms = (time.perf_counter() - t0) * 1000.0
        prev = self._current.recall_stages_ms.get(stage, 0.0)
        self._current.recall_stages_ms[stage] = prev + ms
        self._current.recall_total_ms += ms
        self._current.recall_n_results += max(0, int(n_results))
        return ms

    # ---- audit ----
    def audit_message_layout(
        self,
        messages: list[UnifiedMessage],
        plan: SoulV6BudgetPlan | None,
        token_counter: Any,
        context_window_cap: int = 0,
    ) -> None:
        """计算每个 tenant 的实际 token 用量并写入当前 metrics"""
        if not messages:
            return
        # 找最后一条 user 消息当作 current_turn
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if getattr(messages[i], "role", "") == "user":
                last_user_idx = i
                break

        tenant_tokens: dict[str, int] = {t: 0 for t in ALL_TENANTS}
        for i, m in enumerate(messages):
            tenant = classify_message_tenant(m, is_last_user=(i == last_user_idx))
            try:
                text = getattr(m, "text", "") or ""
                tool_results = getattr(m, "tool_results", None) or []
                tr_text = "\n".join(
                    (getattr(tr, "content", "") or "") for tr in tool_results
                )
                full = (text + "\n" + tr_text).strip()
                n = token_counter.count_tokens(full) if full else 0
            except Exception:
                n = 0
            tenant_tokens[tenant] = tenant_tokens.get(tenant, 0) + n

        self._current.tenant_tokens = tenant_tokens
        if plan is not None:
            self._current.tenant_budget = {
                t: int(plan.get(t) or 0) for t in ALL_TENANTS
            }
        self._current.total_tokens = sum(tenant_tokens.values())
        if context_window_cap:
            self._current.context_window_cap = int(context_window_cap)
