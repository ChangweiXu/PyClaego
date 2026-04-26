"""TaskManager 模块 - Agent 任务管理核心

提供任务状态追踪、订阅分发、层级任务管理功能。

主要组件:
- TaskStatus/TaskType/EventType: 枚举定义
- TaskSubscriber: 订阅者协议
- TaskNode: 任务节点抽象基类
- Task: 任务数据类
- TaskEvent: 事件数据类
- TaskManager: 任务管理器单例
- SessionTaskHandler: Session 级别的任务更新 Handler
- TextSubscriber: 文本文件订阅者（任务树实时导出）
"""

from .base import (
    TaskStatus,
    TaskType,
    EventType,
    TaskSubscriber,
    BaseSubscriber,
    TaskNode,
)
from .task import Task
from .event import TaskEvent
from .manager import TaskManager
from .handler import SessionTaskHandler, SessionTaskHandlerV2
from .text_subscriber import TextSubscriber
from .artifact_store import (
    TaskArtifactStore,
    ArtifactReporter,
    ArtifactRef,
    KIND_LLM_RESPONSE,
    KIND_TOOL_ARGS,
    KIND_TOOL_RESULT,
    KIND_ERROR_TRACE,
    KIND_META,
    KIND_FILE_EDIT,
)

__all__ = [
    # 枚举
    "TaskStatus",
    "TaskType",
    "EventType",
    # 协议与抽象类
    "TaskSubscriber",
    "BaseSubscriber",
    "TaskNode",
    # 数据类
    "Task",
    "TaskEvent",
    # 核心类
    "TaskManager",
    "SessionTaskHandler",
    "SessionTaskHandlerV2",
    # 订阅者实现
    "TextSubscriber",
    # 工件存储
    "TaskArtifactStore",
    "ArtifactReporter",
    "ArtifactRef",
    "KIND_LLM_RESPONSE",
    "KIND_TOOL_ARGS",
    "KIND_TOOL_RESULT",
    "KIND_ERROR_TRACE",
    "KIND_META",
    "KIND_FILE_EDIT",
]
