"""Task 数据类 - 任务节点实现

包含:
- generate_task_id(): 任务 ID 生成函数
- Task: 任务数据类
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import TaskStatus, TaskType
from .belonging import TaskBelonging


def _coerce_belonging(value: str | TaskBelonging | dict[str, Any] | None) -> TaskBelonging | None:
    """把 str / dict / TaskBelonging 都归一为 TaskBelonging（None 透传）。"""
    if value is None:
        return None
    if isinstance(value, TaskBelonging):
        return value
    if isinstance(value, dict):
        return TaskBelonging.from_dict(value)
    if isinstance(value, str):
        if not value:
            return None
        return TaskBelonging(ps_id=value)
    raise TypeError(f"Cannot coerce {type(value).__name__} to TaskBelonging")


def generate_task_id(session_id_or_belonging: str | TaskBelonging) -> str:
    """生成任务 ID

    格式: {key}-{YYYYMMDD_HHMMSS}-{uuid[:4]}

    Args:
        session_id_or_belonging: 旧式 session_id 字符串，或新的 TaskBelonging。
            字符串视为 ``TaskBelonging(ps_id=session_id)``，得到的 key 等于
            原 session_id，因此对旧调用方完全兼容。

    Examples:
        >>> generate_task_id("default_session")
        'default_session-20260410_120000-a3f9'
        >>> generate_task_id(TaskBelonging("alice", "w_papers"))
        'alice__w_papers-20260410_120000-b7c2'
    """
    if isinstance(session_id_or_belonging, TaskBelonging):
        key = session_id_or_belonging.key()
    else:
        key = session_id_or_belonging
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:4]
    return f"{key}-{date_str}-{short_uuid}"


@dataclass
class Task:
    """任务数据类

    表示任务树中的一个节点，包含任务的基本信息、状态、时间戳等。
    支持父子关系建立和序列化导出。

    PersonalSpace 模型迁移说明：
    - 字段名 ``session_id`` 保留为兼容字段，新代码应使用 ``belongs_to``
      （类型 ``TaskBelonging``）来表达 ``(ps_id, widget_id?, subagent_id?)``
      的归属。
    - 当只传 ``session_id`` 时，会自动包成 ``TaskBelonging(ps_id=session_id)``，
      ``belongs_to.key() == session_id``，调用方无感。
    - 当显式传 ``belongs_to`` 时，``session_id`` 会被覆盖为 ``belongs_to.key()``。
    """

    # 必需字段(无默认值)
    task_id: str
    session_id: str
    task_type: TaskType
    name: str
    status: TaskStatus
    created_at: datetime

    # 可选字段(有默认值) - 必须在必需字段后面
    started_at: datetime | None = None
    completed_at: datetime | None = None
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    progress: float = 0.0
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    seq: int = 0  # 在 parent 下的单调递增序号（同一 parent 下从 0 开始）
    belongs_to: TaskBelonging | None = None

    # 内部字段(不序列化)
    _parent: Task | None = field(default=None, repr=False, compare=False)
    _children: list[Task] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self):
        # belongs_to / session_id 双向同步：以显式 belongs_to 为准
        coerced = _coerce_belonging(self.belongs_to) if self.belongs_to is not None \
            else _coerce_belonging(self.session_id)
        if coerced is None:
            raise ValueError("Task requires session_id or belongs_to")
        self.belongs_to = coerced
        self.session_id = coerced.key()
        # 验证进度范围
        if not (0.0 <= self.progress <= 1.0):
            raise ValueError(f"Task progress must be in [0.0, 1.0], got {self.progress}")

    # ─────────────────────────────────────────────────────────
    # TaskNode 协议实现
    # ─────────────────────────────────────────────────────────

    @property
    def parent(self) -> Task | None:
        return self._parent

    @property
    def children(self) -> list[Task]:
        return self._children

    def to_dict(self) -> dict[str, Any]:
        """递归序列化为字典（包含 belongs_to）。"""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "belongs_to": self.belongs_to.to_dict() if self.belongs_to else None,
            "task_type": self.task_type.value,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "parent_id": self.parent_id,
            "children_ids": self.children_ids.copy(),
            "progress": self.progress,
            "description": self.description,
            "metadata": self.metadata.copy(),
            "error": self.error,
            "seq": self.seq,
        }

    # ─────────────────────────────────────────────────────────
    # 任务树操作
    # ─────────────────────────────────────────────────────────

    def add_child(self, child: Task) -> None:
        if child.task_id not in self.children_ids:
            self.children_ids.append(child.task_id)
            self._children.append(child)
            child._parent = self
            child.parent_id = self.task_id

    def remove_child(self, child_id: str) -> None:
        if child_id in self.children_ids:
            self.children_ids.remove(child_id)
            self._children = [c for c in self._children if c.task_id != child_id]

    def get_depth(self) -> int:
        depth = 0
        current = self._parent
        while current is not None:
            depth += 1
            current = current._parent
        return depth

    def get_root(self) -> Task:
        current = self
        while current._parent is not None:
            current = current._parent
        return current

    def is_leaf(self) -> bool:
        return len(self.children_ids) == 0

    def is_finished(self) -> bool:
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )
