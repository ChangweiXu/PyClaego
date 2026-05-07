"""TaskBelonging - 任务归属标识

任务不再单纯归属于一个 Session；它属于：
- 一个 PersonalSpace（必填）
- 可选的 Widget（绝大多数情况都会有）
- 可选的 SubAgent（在 widget 内部由 spawn_subagent 触发的执行）

约束：
- ``subagent_id`` 必须配合 ``widget_id`` 使用。
- ``key()`` 用 ``__`` 作为分隔符，便于嵌入 task_id / 文件路径。

本数据类是 frozen + slot-friendly 的；用作 dict key 时按 (ps, widget, subagent)
比较；序列化通过显式 ``to_dict`` / ``from_dict`` 处理（递归调用兼容嵌套场景）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TaskBelonging:
    """任务归属三元组

    Attributes:
        ps_id: PersonalSpace ID（必填）
        widget_id: Widget ID（可选；为 None 表示 PS 级任务，例如 PS 协调任务）
        subagent_id: SubAgent ID（可选；表示该任务来源于一个子 Agent 调用）
    """

    ps_id: str
    widget_id: str | None = None
    subagent_id: str | None = None

    def __post_init__(self) -> None:
        if not self.ps_id:
            raise ValueError("TaskBelonging.ps_id is required")
        if self.subagent_id and not self.widget_id:
            raise ValueError(
                "TaskBelonging.subagent_id requires widget_id to be set"
            )

    # ------------------------------------------------------------------
    # 字符串化
    # ------------------------------------------------------------------

    def key(self) -> str:
        """返回稳定的字符串键，用于索引、文件路径、task_id 拼接。

        Examples:
            TaskBelonging("alice").key()                  -> "alice"
            TaskBelonging("alice","w1").key()             -> "alice__w1"
            TaskBelonging("alice","w1","sa_xx").key()     -> "alice__w1__sa_xx"
        """
        parts = [self.ps_id]
        if self.widget_id:
            parts.append(self.widget_id)
        if self.subagent_id:
            parts.append(self.subagent_id)
        return "__".join(parts)

    def widget_key(self) -> str:
        """返回到 widget 层为止的键（忽略 subagent_id）。

        用于按 widget 聚合任务（例如内存配额、PS-side 索引）。
        若没有 widget_id，则等价于 ``ps_id``。
        """
        return f"{self.ps_id}__{self.widget_id}" if self.widget_id else self.ps_id

    # ------------------------------------------------------------------
    # 派生
    # ------------------------------------------------------------------

    def with_subagent(self, subagent_id: str) -> TaskBelonging:
        """返回挂上指定 subagent_id 的新归属（不可变副本）。"""
        if not self.widget_id:
            raise ValueError(
                "Cannot derive subagent belonging without widget_id"
            )
        return TaskBelonging(
            ps_id=self.ps_id,
            widget_id=self.widget_id,
            subagent_id=subagent_id,
        )

    def without_subagent(self) -> TaskBelonging:
        """返回去掉 subagent_id 的新归属（用于回到 widget 层）。"""
        return TaskBelonging(ps_id=self.ps_id, widget_id=self.widget_id)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "ps_id": self.ps_id,
            "widget_id": self.widget_id,
            "subagent_id": self.subagent_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> TaskBelonging | None:
        if not d:
            return None
        return TaskBelonging(
            ps_id=d["ps_id"],
            widget_id=d.get("widget_id"),
            subagent_id=d.get("subagent_id"),
        )

    # ------------------------------------------------------------------
    # 兼容遗留（旧代码读取 session_id 时拿到的就是 key()）
    # ------------------------------------------------------------------

    @property
    def legacy_session_id(self) -> str:
        return self.key()
