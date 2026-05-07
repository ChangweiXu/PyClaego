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

from .artifact_store import (
    KIND_ERROR_TRACE,
    KIND_FILE_EDIT,
    KIND_LLM_RESPONSE,
    KIND_META,
    KIND_STREAM_CONTENT,
    KIND_TOOL_ARGS,
    KIND_TOOL_RESULT,
    ArtifactRef,
    ArtifactReporter,
    TaskArtifactStore,
    safe_attach,
)
from .base import (
    BaseSubscriber,
    EventType,
    TaskNode,
    TaskStatus,
    TaskSubscriber,
    TaskType,
)
from .belonging import TaskBelonging
from .event import TaskEvent
from .handler import (
    SessionTaskHandler,
    SessionTaskHandlerV2,
    WidgetTaskHandler,
    # TaskHandler,  # 向后兼容别名，== WidgetTaskHandler
)
from .manager import TaskManager
from .task import Task, generate_task_id
from .text_subscriber import TextSubscriber

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
    "TaskBelonging",
    "generate_task_id",
    # 核心类
    "TaskManager",
    "SessionTaskHandler",
    "SessionTaskHandlerV2",
    "WidgetTaskHandler",
    # "TaskHandler",
    # 订阅者实现
    "TextSubscriber",
    # 工件存储
    "TaskArtifactStore",
    "ArtifactReporter",
    "ArtifactRef",
    "safe_attach",
    "KIND_LLM_RESPONSE",
    "KIND_TOOL_ARGS",
    "KIND_TOOL_RESULT",
    "KIND_ERROR_TRACE",
    "KIND_META",
    "KIND_FILE_EDIT",
    "KIND_STREAM_CONTENT",
]
