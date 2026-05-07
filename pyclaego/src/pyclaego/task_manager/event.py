"""TaskEvent 数据类 - 任务事件定义

包含:
- TaskEvent: 任务事件数据类
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import EventType
from .belonging import TaskBelonging


@dataclass
class TaskEvent:
    """任务事件数据类

    表示任务状态变化时产生的事件，用于通知订阅者。

    PersonalSpace 模型迁移：
    - 兼容字段 ``session_id`` 保留为字符串
    - 新增 ``belongs_to: Optional[TaskBelonging]``，新订阅方应优先使用
    """

    event_type: EventType
    session_id: str
    task_id: str
    timestamp: datetime
    task_snapshot: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    belongs_to: TaskBelonging | None = None

    def __post_init__(self) -> None:
        if self.belongs_to is None and self.session_id:
            self.belongs_to = TaskBelonging(ps_id=self.session_id)
        elif self.belongs_to is not None:
            # 以 belongs_to 为准回填 session_id
            self.session_id = self.belongs_to.key()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.name,
            "session_id": self.session_id,
            "belongs_to": self.belongs_to.to_dict() if self.belongs_to else None,
            "task_id": self.task_id,
            "timestamp": self.timestamp.isoformat(),
            "task_snapshot": self.task_snapshot.copy(),
            "extra": self.extra.copy(),
        }

