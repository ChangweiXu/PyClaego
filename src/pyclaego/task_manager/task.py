"""Task 数据类 - 任务节点实现

包含:
- generate_task_id(): 任务 ID 生成函数
- Task: 任务数据类
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import TaskStatus, TaskType


def generate_task_id(session_id: str) -> str:
    """生成任务 ID
    
    格式: {session_id}-{YYYYMMDD_HHMMSS}-{uuid[:4]}
    
    Args:
        session_id: Session ID
        
    Returns:
        任务 ID
        
    Examples:
        >>> generate_task_id("default_session")
        'default_session-20260410-a3f9'
        >>> generate_task_id("feishu_chat_001")
        'feishu_chat_001-20260410-b7c2'
    """
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:4]
    return f"{session_id}-{date_str}-{short_uuid}"


@dataclass
class Task:
    """任务数据类
    
    表示任务树中的一个节点,包含任务的基本信息、状态、时间戳等。
    支持父子关系建立和序列化导出。
    
    Attributes:
        task_id: 任务唯一标识
        session_id: 所属 Session ID
        task_type: 任务类型
        name: 任务名称
        status: 任务状态
        created_at: 创建时间
        started_at: 开始时间
        completed_at: 完成时间
        parent_id: 父任务ID
        children_ids: 子任务ID列表
        progress: 进度 (0.0 ~ 1.0)
        description: 任务描述
        metadata: 额外元数据
        error: 错误信息(仅失败时)
    """
    
    # 必需字段(无默认值)
    task_id: str
    session_id: str
    task_type: TaskType
    name: str
    status: TaskStatus
    created_at: datetime
    
    # 可选字段(有默认值) - 必须在必需字段后面
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    progress: float = 0.0
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    seq: int = 0  # 在 parent 下的单调递增序号（同一 parent 下从 0 开始）
    
    # 内部字段(不序列化)
    _parent: Optional["Task"] = field(default=None, repr=False, compare=False)
    _children: List["Task"] = field(default_factory=list, repr=False, compare=False)
    
    def __post_init__(self):
        """初始化后处理 - 验证字段"""
        # 验证进度范围
        if not (0.0 <= self.progress <= 1.0):
            raise ValueError(f"Task progress must be in [0.0, 1.0], got {self.progress}")
    
    # ─────────────────────────────────────────────────────────
    # TaskNode 协议实现
    # ─────────────────────────────────────────────────────────
    
    @property
    def parent(self) -> Optional["Task"]:
        """父任务节点"""
        return self._parent
    
    @property
    def children(self) -> List["Task"]:
        """子任务节点列表"""
        return self._children
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典
        
        Returns:
            任务信息字典,包含所有公开字段
        """
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
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
    
    def add_child(self, child: "Task") -> None:
        """添加子任务
        
        Args:
            child: 子任务对象
        """
        if child.task_id not in self.children_ids:
            self.children_ids.append(child.task_id)
            self._children.append(child)
            child._parent = self
            child.parent_id = self.task_id
    
    def remove_child(self, child_id: str) -> None:
        """移除子任务
        
        Args:
            child_id: 子任务ID
        """
        if child_id in self.children_ids:
            self.children_ids.remove(child_id)
            self._children = [c for c in self._children if c.task_id != child_id]
    
    def get_depth(self) -> int:
        """获取任务在树中的深度
        
        Returns:
            深度 (0表示根任务)
        """
        depth = 0
        current = self._parent
        while current is not None:
            depth += 1
            current = current._parent
        return depth
    
    def get_root(self) -> "Task":
        """获取根任务
        
        Returns:
            根任务对象
        """
        current = self
        while current._parent is not None:
            current = current._parent
        return current
    
    def is_leaf(self) -> bool:
        """判断是否为叶子节点
        
        Returns:
            True 表示没有子任务
        """
        return len(self.children_ids) == 0
    
    def is_finished(self) -> bool:
        """判断任务是否已结束(完成/失败/取消)
        
        Returns:
            True 表示任务已结束
        """
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )
