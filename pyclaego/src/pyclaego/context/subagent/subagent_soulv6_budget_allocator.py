"""SubAgentSoulV6BudgetAllocator — 子 Agent 显式 token 预算分配器

4 个 tenant（相比 SoulV6 的 10 个大幅精简，适合子 Agent 场景）：

    system              系统提示词
    current_tool_results 当前轮进行中的工具结果
    history_verbatim    近期原始历史消息
    reserved_output     为 LLM 回复 / reasoning 预留

用法：
    allocator = SubAgentSoulV6BudgetAllocator.from_config(config)
    plan = allocator.allocate(total_window=100_000)
    # plan: {"system": 4096, "current_tool_results": 20000, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubAgentSoulV6TenantSpec:
    name: str
    min_tokens: int
    max_tokens: int
    priority: int  # 数值越大越优先


DEFAULT_TENANTS: list[SubAgentSoulV6TenantSpec] = [
    # name                    min      max   priority
    SubAgentSoulV6TenantSpec("system",                2_000,   8_000, 100),
    SubAgentSoulV6TenantSpec("current_tool_results",  2_000,  30_000,  90),
    SubAgentSoulV6TenantSpec("history_verbatim",      1_000,  15_000,  80),
    SubAgentSoulV6TenantSpec("reserved_output",       2_000,   8_000,  70),
]


@dataclass
class SubAgentSoulV6BudgetPlan:
    total_window: int
    caps: dict[str, int] = field(default_factory=dict)

    def get(self, tenant: str, default: int = 0) -> int:
        return self.caps.get(tenant, default)

    def as_dict(self) -> dict[str, int]:
        return dict(self.caps)


class SubAgentSoulV6BudgetAllocator:
    """子 Agent token 预算分配器"""

    def __init__(self, tenants: list[SubAgentSoulV6TenantSpec] | None = None) -> None:
        self.tenants: list[SubAgentSoulV6TenantSpec] = list(
            tenants or DEFAULT_TENANTS
        )
        self.tenants.sort(key=lambda t: t.priority, reverse=True)

    @classmethod
    def from_config(cls, config: dict) -> SubAgentSoulV6BudgetAllocator:
        """从 dict 配置构建

        配置格式（可选；缺省使用内置 4 tenant）：
            tenants:
              - name: system
                min_tokens: 2000
                max_tokens: 8000
                priority: 100
        """
        tenants_cfg = config.get("tenants") if isinstance(config, dict) else None
        if not tenants_cfg:
            return cls()
        tenants: list[SubAgentSoulV6TenantSpec] = []
        for item in tenants_cfg:
            tenants.append(SubAgentSoulV6TenantSpec(
                name=item["name"],
                min_tokens=int(item.get("min_tokens", 0)),
                max_tokens=int(item.get("max_tokens", 1_000_000)),
                priority=int(item.get("priority", 0)),
            ))
        return cls(tenants)

    def allocate(self, total_window: int = 100_000) -> SubAgentSoulV6BudgetPlan:
        """按 tenant 分配预算

        1. 先给每个 tenant 分配 min_tokens（若总 min 超预算，按比例缩减）
        2. 按 priority 降序把剩余预算逐个分到 max_tokens
        """
        if total_window <= 0:
            return SubAgentSoulV6BudgetPlan(
                total_window=0, caps={t.name: 0 for t in self.tenants}
            )

        total_min = sum(t.min_tokens for t in self.tenants)
        caps: dict[str, int] = {}

        if total_min > total_window:
            scale = total_window / max(total_min, 1)
            for t in self.tenants:
                caps[t.name] = int(t.min_tokens * scale)
            return SubAgentSoulV6BudgetPlan(total_window=total_window, caps=caps)

        for t in self.tenants:
            caps[t.name] = t.min_tokens

        remaining = total_window - total_min
        for t in self.tenants:  # 已按 priority 降序
            if remaining <= 0:
                break
            room = t.max_tokens - caps[t.name]
            if room <= 0:
                continue
            grant = min(room, remaining)
            caps[t.name] += grant
            remaining -= grant

        return SubAgentSoulV6BudgetPlan(total_window=total_window, caps=caps)

    def describe(self) -> str:
        lines = [f"{'tenant':<25} {'min':>8} {'max':>8} {'prio':>6}"]
        for t in self.tenants:
            lines.append(
                f"{t.name:<25} {t.min_tokens:>8} {t.max_tokens:>8} {t.priority:>6}"
            )
        return "\n".join(lines)
