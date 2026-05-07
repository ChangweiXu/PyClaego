"""SoulV6BudgetAllocator — 显式分 tenant 的 token 预算分配器

用于替代 V5 中硬编码的 `context_window_cap // 2` 等粗放式分配。

设计要点：
- 每个 tenant 有 (min_tokens, max_tokens, priority)
- 总预算先满足所有 tenant 的 min，再按 priority 降序分配剩余额度
- 任何 tenant 分到的额度不超过其 max
- 剩余预算（若有）作为 reserved_reasoning 的保留额度（不再分给其他 tenant）

典型 tenant：
    system               系统提示
    preferences          用户/项目偏好
    recall               召回的 case/experience
    entity_cards         实体卡片
    open_loops           未闭合的历史问题
    history_briefs       旧轮次的 TurnBrief
    history_verbatim     近期原始消息
    tool_results_live    当前进行中的工具结果
    current_turn         当前用户输入
    reserved_reasoning   为 reasoning / LLM 回复预留（不装载内容）

使用示例：
    allocator = SoulV6BudgetAllocator.from_config(config)
    plan = allocator.allocate(total_window=131072)
    # plan: {"system": 4096, "preferences": 2000, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SoulV6TenantSpec:
    """单个 tenant 的预算规格"""
    name: str
    min_tokens: int
    max_tokens: int
    priority: int  # 数值越大，优先级越高（先分配）


# 默认 tenant 列表（可通过配置覆盖）
DEFAULT_TENANTS: list[SoulV6TenantSpec] = [
    # name                  min      max     priority
    SoulV6TenantSpec("system",             2_000,  8_000,  100),
    SoulV6TenantSpec("preferences",          500,  3_000,   90),
    SoulV6TenantSpec("recall",             1_000, 12_000,   80),
    SoulV6TenantSpec("entity_cards",         500,  4_000,   70),
    SoulV6TenantSpec("open_loops",           200,  2_000,   65),
    SoulV6TenantSpec("history_briefs",       500,  8_000,   60),
    SoulV6TenantSpec("history_verbatim",   2_000, 30_000,   55),
    SoulV6TenantSpec("tool_results_live",  4_000, 40_000,   50),
    SoulV6TenantSpec("current_turn",       1_000, 20_000,   95),
    SoulV6TenantSpec("reserved_reasoning", 4_000, 16_000,   30),
]


@dataclass
class SoulV6BudgetPlan:
    """分配结果"""
    total_window: int
    caps: dict[str, int] = field(default_factory=dict)  # tenant_name → token_cap

    def get(self, tenant: str, default: int = 0) -> int:
        return self.caps.get(tenant, default)

    def as_dict(self) -> dict[str, int]:
        return dict(self.caps)


class SoulV6BudgetAllocator:
    """按 tenant 分配 token 预算"""

    def __init__(self, tenants: list[SoulV6TenantSpec] | None = None) -> None:
        self.tenants: list[SoulV6TenantSpec] = list(tenants or DEFAULT_TENANTS)
        # 校验：按 priority 降序排序
        self.tenants.sort(key=lambda t: t.priority, reverse=True)

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict) -> SoulV6BudgetAllocator:
        """从 dict 配置构建 allocator

        配置格式：
            tenants:
              - name: system
                min_tokens: 2000
                max_tokens: 8000
                priority: 100
              - ...
        """
        tenants_cfg = config.get("tenants") if isinstance(config, dict) else None
        if not tenants_cfg:
            return cls()
        tenants: list[SoulV6TenantSpec] = []
        for item in tenants_cfg:
            tenants.append(SoulV6TenantSpec(
                name=item["name"],
                min_tokens=int(item.get("min_tokens", 0)),
                max_tokens=int(item.get("max_tokens", 1_000_000)),
                priority=int(item.get("priority", 0)),
            ))
        return cls(tenants)

    # ------------------------------------------------------------------
    # 核心分配
    # ------------------------------------------------------------------

    def allocate(self, total_window: int) -> SoulV6BudgetPlan:
        """按 tenant 分配预算

        算法：
        1. 先给每个 tenant 分配 min_tokens（若总 min 超预算，按比例缩减）
        2. 按 priority 降序，把剩余预算逐个分到 tenant 上限（max_tokens）
        3. 返回每个 tenant 的最终 cap

        Args:
            total_window: 总上下文窗口 token 数

        Returns:
            SoulV6BudgetPlan
        """
        if total_window <= 0:
            return SoulV6BudgetPlan(total_window=0, caps={t.name: 0 for t in self.tenants})

        # Step 1: min 分配
        total_min = sum(t.min_tokens for t in self.tenants)
        caps: dict[str, int] = {}

        if total_min > total_window:
            # min 总和超预算，按比例缩减
            scale = total_window / max(total_min, 1)
            for t in self.tenants:
                caps[t.name] = int(t.min_tokens * scale)
            return SoulV6BudgetPlan(total_window=total_window, caps=caps)

        for t in self.tenants:
            caps[t.name] = t.min_tokens

        # Step 2: 剩余预算按 priority 降序分配（每个 tenant 提高到 max_tokens）
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

        return SoulV6BudgetPlan(total_window=total_window, caps=caps)

    # ------------------------------------------------------------------
    # 调试 / 日志
    # ------------------------------------------------------------------

    def describe(self) -> str:
        """返回人类可读的 tenant 配置描述"""
        lines = [f"{'tenant':<20} {'min':>8} {'max':>8} {'prio':>6}"]
        for t in self.tenants:
            lines.append(
                f"{t.name:<20} {t.min_tokens:>8} {t.max_tokens:>8} {t.priority:>6}"
            )
        return "\n".join(lines)
