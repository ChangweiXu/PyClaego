"""TaskManager 基础定义 - 枚举、协议、抽象类

包含:
- TaskStatus: 任务状态枚举
- TaskType: 任务类型枚举
- EventType: 事件类型枚举
- TaskSubscriber: 订阅者协议
- BaseSubscriber: 订阅者抽象基类
- TaskNode: 任务节点抽象基类
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Protocol, Set


# ============================================================================
# 枚举定义
# ============================================================================

class TaskStatus(Enum):
    """任务状态枚举"""
    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 正在执行
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 执行失败
    CANCELLED = "cancelled"   # 已取消


class TaskType(Enum):
    """任务类型枚举"""
    USER_MESSAGE = "user_message"         # 用户消息处理(顶层任务)
    AGENT_LOOP = "agent_loop"             # Agent 循环
    TOOL_EXECUTION = "tool_execution"     # 工具调用
    SUBAGENT_SPAWN = "subagent_spawn"     # 子Agent创建
    SUBAGENT_LOOP = "subagent_loop"       # 子Agent循环
    LLM_CALL = "llm_call"                 # LLM 调用(可选)
    MEMORY_COMPRESS = "memory_compress"   # 记忆压缩(自动/手动)
    MEMORY_RECALL = "memory_recall"       # 记忆召回(自动/手动)
    # SoulV6 新增
    MEMORY_BUDGET = "memory_budget"               # V6: token 预算分配
    MEMORY_BRIEF = "memory_brief"                 # V6: TurnBrief 合成
    MEMORY_WRITE_REVIEW = "memory_write_review"   # V6: 记忆写入评审
    MEMORY_EVICT = "memory_evict"                 # V6: 过时工具结果驱逐


class EventType(Enum):
    """事件类型枚举"""
    # Session 级别事件
    SESSION_CREATED = auto()
    SESSION_STARTED = auto()
    SESSION_COMPLETED = auto()
    SESSION_FAILED = auto()
    SESSION_CANCELLED = auto()
    
    # Task 级别事件
    TASK_CREATED = auto()
    TASK_STARTED = auto()
    TASK_PROGRESS = auto()
    TASK_COMPLETED = auto()
    TASK_FAILED = auto()
    TASK_CANCELLED = auto()
    TASK_LOG = auto()           # 任务日志事件


# ============================================================================
# 协议定义
# ============================================================================

class TaskSubscriber(Protocol):
    """任务订阅者协议 - 定义订阅者需要实现的接口
    
    订阅者通过实现此协议,可以接收 TaskManager 发送的任务事件。
    
    **注意**: 自 v2.0 起，on_event() 改为异步方法。
    """
    
    async def on_event(self, event: "TaskEvent") -> None:
        """接收任务事件（异步）
        
        Args:
            event: 任务事件对象
        """
        ...
    
    def get_subscriber_id(self) -> str:
        """获取订阅者唯一标识
        
        Returns:
            订阅者ID (用于注销等操作)
        """
        ...
    
    def get_subscribed_events(self) -> Set[EventType]:
        """获取订阅的事件类型集合
        
        Returns:
            事件类型集合,空集合表示订阅所有事件
        """
        ...


# ============================================================================
# 抽象基类
# ============================================================================

class BaseSubscriber(ABC):
    """订阅者抽象基类 - 提供默认实现
    
    继承此类可以快速实现一个订阅者,只需重写 on_event() 方法。
    """
    
    def __init__(self, subscriber_id: Optional[str] = None):
        """初始化订阅者
        
        Args:
            subscriber_id: 订阅者ID,如果为None则自动生成UUID
        """
        self._subscriber_id = subscriber_id or str(uuid.uuid4())
        self._subscribed_events: Set[EventType] = set()  # 空集合=订阅所有
    
    def get_subscriber_id(self) -> str:
        """获取订阅者ID"""
        return self._subscriber_id
    
    def get_subscribed_events(self) -> Set[EventType]:
        """获取订阅的事件类型集合"""
        return self._subscribed_events
    
    def subscribe_to(self, *events: EventType) -> None:
        """添加事件订阅
        
        Args:
            *events: 要订阅的事件类型
        """
        self._subscribed_events.update(events)
    
    def unsubscribe_from(self, *events: EventType) -> None:
        """取消事件订阅
        
        Args:
            *events: 要取消订阅的事件类型
        """
        self._subscribed_events.difference_update(events)
    
    @abstractmethod
    async def on_event(self, event: "TaskEvent") -> None:
        """处理接收到的事件 - 子类必须实现（异步）
        
        Args:
            event: 任务事件对象
        """
        pass


class TaskNode(ABC):
    """任务节点抽象基类 - 定义任务树的基本结构
    
    提供任务树操作的统一接口,实际实现在 Task 类中。
    """
    
    @property
    @abstractmethod
    def task_id(self) -> str:
        """任务唯一标识"""
        pass
    
    @property
    @abstractmethod
    def status(self) -> TaskStatus:
        """任务状态"""
        pass
    
    @property
    @abstractmethod
    def task_type(self) -> TaskType:
        """任务类型"""
        pass
    
    @property
    @abstractmethod
    def parent(self) -> Optional["TaskNode"]:
        """父任务节点"""
        pass
    
    @property
    @abstractmethod
    def children(self) -> List["TaskNode"]:
        """子任务节点列表"""
        pass
    
    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典,用于导出
        
        Returns:
            任务信息字典
        """
        pass
