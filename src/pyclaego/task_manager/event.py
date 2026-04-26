"""TaskEvent 数据类 - 任务事件定义

包含:
- TaskEvent: 任务事件数据类
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from .base import EventType


@dataclass
class TaskEvent:
    """任务事件数据类
    
    表示任务状态变化时产生的事件,用于通知订阅者。
    
    Attributes:
        event_type: 事件类型
        session_id: 所属 Session ID
        task_id: 任务ID
        timestamp: 事件时间戳
        task_snapshot: 任务状态快照 (Task.to_dict() 结果)
        extra: 额外信息
    """
    
    event_type: EventType
    session_id: str
    task_id: str
    timestamp: datetime
    task_snapshot: Dict[str, Any]
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典
        
        Returns:
            事件信息字典
        """
        return {
            "event_type": self.event_type.name,  # 使用枚举名称
            "session_id": self.session_id,
            "task_id": self.task_id,
            "timestamp": self.timestamp.isoformat(),
            "task_snapshot": self.task_snapshot.copy(),
            "extra": self.extra.copy(),
        }
