"""TaskManager 核心管理器 - 任务管理单例

包含:
- TaskManager: 任务管理器单例类
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from ..config import get_config
from ..logging import get_running_log
from .base import EventType, TaskStatus, TaskSubscriber, TaskType
from .event import TaskEvent
from .task import Task, generate_task_id

_rlog = get_running_log()


class TaskManager:
    """任务管理器 (Singleton)
    
    功能:
    - 管理所有 Session 的任务状态
    - 支持任务生命周期管理(创建/开始/更新/完成/失败/取消)
    - 订阅-发布模式通知订阅者
    - 导出任务状态供 UI 展示
    - 支持任务树结构(父子关系)
    
    使用示例:
        >>> task_manager = TaskManager.get_instance()
        >>> task_id = task_manager.create_task(
        ...     session_id="sess_001",
        ...     task_type=TaskType.USER_MESSAGE,
        ...     name="User: 帮我分析代码",
        ... )
        >>> task_manager.start_task(task_id)
        >>> task_manager.complete_task(task_id)
    """
    
    _instance: Optional["TaskManager"] = None
    _lock = threading.Lock()
    
    @classmethod
    def get_instance(cls) -> "TaskManager":
        """获取单例实例
        
        Returns:
            TaskManager 实例
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def __init__(self):
        """初始化 TaskManager (私有,仅供单例使用)"""
        if TaskManager._instance is not None:
            raise RuntimeError("TaskManager is a singleton, use get_instance() instead")
        
        # 任务存储
        self._tasks: Dict[str, Task] = {}  # task_id -> Task
        self._session_tasks: Dict[str, List[str]] = {}  # session_id -> [task_ids]
        
        # 订阅者管理
        self._subscribers: List[TaskSubscriber] = []
        
        # 异步锁(用于并发安全)
        self._update_lock = asyncio.Lock()
        
        # 配置
        config = get_config()
        self._config = config.get("task_manager", {})
        self._max_memory_tasks_per_session = self._config.get("max_memory_tasks_per_session", 100)
        self._max_task_depth = self._config.get("max_task_depth", 10)
        
        _rlog.info("task_manager", "[TaskManager] 初始化完成")
    
    # ─────────────────────────────────────────────────────────
    # 任务生命周期管理
    # ─────────────────────────────────────────────────────────
    
    async def create_task(
        self,
        session_id: str,
        task_type: TaskType,
        name: str,
        parent_id: Optional[str] = None,
        description: str = "",
        **metadata,
    ) -> str:
        """创建新任务
        
        Args:
            session_id: Session ID
            task_type: 任务类型
            name: 任务名称
            parent_id: 父任务ID (可选)
            description: 任务描述
            **metadata: 额外元数据
            
        Returns:
            任务ID
            
        Raises:
            ValueError: 父任务不存在或任务深度超限
        """
        # 验证父任务存在
        parent_task = None
        if parent_id is not None:
            parent_task = self._tasks.get(parent_id)
            if parent_task is None:
                raise ValueError(f"Parent task not found: {parent_id}")
            
            # 检查任务深度
            depth = parent_task.get_depth() + 1
            if depth >= self._max_task_depth:
                raise ValueError(
                    f"Task depth exceeds limit: {depth} >= {self._max_task_depth}"
                )
        
        # 生成任务ID
        task_id = generate_task_id(session_id)

        # 计算 seq：同 parent 下现有子任务数（根任务下：session 内根任务计数）
        if parent_task is not None:
            seq = len(parent_task.children_ids)
        else:
            seq = sum(
                1 for tid in self._session_tasks.get(session_id, [])
                if self._tasks.get(tid) and self._tasks[tid].parent_id is None
            )

        # 创建任务对象
        task = Task(
            task_id=task_id,
            session_id=session_id,
            task_type=task_type,
            name=name,
            status=TaskStatus.PENDING,
            created_at=datetime.now(),
            parent_id=parent_id,
            description=description,
            metadata=metadata,
            seq=seq,
        )
        
        # 建立父子关系
        if parent_task is not None:
            parent_task.add_child(task)
        
        # 存储任务
        self._tasks[task_id] = task
        
        # 更新 Session 任务列表
        if session_id not in self._session_tasks:
            self._session_tasks[session_id] = []
        self._session_tasks[session_id].append(task_id)
        
        # 检查是否需要清理
        self._check_and_cleanup_session(session_id)
        
        # 通知订阅者
        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_CREATED,
            session_id=session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
        ))
        
        _rlog.debug(
            "task_manager",
            f"[TaskManager] 创建任务: {task_id} ({task_type.value}) - {name}"
        )
        
        return task_id
    
    async def start_task(self, task_id: str) -> None:
        """标记任务开始
        
        Args:
            task_id: 任务ID
            
        Raises:
            ValueError: 任务不存在
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        
        # 通知订阅者
        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_STARTED,
            session_id=task.session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
        ))
        
        _rlog.debug(
            "task_manager",
            f"[TaskManager] 任务开始: {task_id} - {task.name}"
        )
    
    async def update_task_progress(
        self,
        task_id: str,
        progress: float,
        message: str = "",
    ) -> None:
        """更新任务进度
        
        Args:
            task_id: 任务ID
            progress: 进度 (0.0 ~ 1.0)
            message: 进度描述
            
        Raises:
            ValueError: 任务不存在或进度超出范围
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        
        if not (0.0 <= progress <= 1.0):
            raise ValueError(f"Progress must be in [0.0, 1.0], got {progress}")
        
        task.progress = progress
        if message:
            task.description = message
        
        # 通知订阅者
        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_PROGRESS,
            session_id=task.session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
            extra={"message": message},
        ))
        
        _rlog.debug(
            "task_manager",
            f"[TaskManager] 任务进度: {task_id} - {progress:.1%} {message}"
        )
    
    async def end_child_tasks(
        self,
        task_id: str,
        result: TaskStatus = TaskStatus.FAILED,
    ) -> None:
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        
        termination_method = {
            TaskStatus.COMPLETED: self.complete_task,
            TaskStatus.FAILED: self.fail_task,
            TaskStatus.CANCELLED: self.cancel_task,
        }[result]

        # 递归结束子任务
        for child_task in task.children:
            if child_task.status not in [
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.COMPLETED,
            ]:
                await termination_method(child_task.task_id, f"Parent task termined while subtask still running.")

                _rlog.warning(
                    "core_service",
                    f"[TaskManager] 强制失败子任务: {child_task.task_id}"
                )
    
    async def complete_task(
        self,
        task_id: str,
        result: Optional[Any] = None,
    ) -> None:
        """标记任务完成
        
        Args:
            task_id: 任务ID
            result: 任务结果 (可选)
            
        Raises:
            ValueError: 任务不存在
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now()
        task.progress = 1.0
        
        if result is not None:
            task.metadata["result"] = result

        # 机械生成 digest（供仪表板 tooltip 使用，不调 LLM）
        task.metadata["digest"] = self._compute_digest(task)

        await self.end_child_tasks(task_id)
        
        # 通知订阅者
        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_COMPLETED,
            session_id=task.session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
        ))
        
        _rlog.debug(
            "task_manager",
            f"[TaskManager] 任务完成: {task_id} - {task.name}"
        )
    
    async def fail_task(self, task_id: str, error: str) -> None:
        """标记任务失败
        
        Args:
            task_id: 任务ID
            error: 错误信息
            
        Raises:
            ValueError: 任务不存在
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        
        task.status = TaskStatus.FAILED
        task.completed_at = datetime.now()
        task.error = error

        # 机械生成 digest（供仪表板 tooltip 使用，不调 LLM）
        task.metadata["digest"] = self._compute_digest(task)

        await self.end_child_tasks(task_id)
        
        # 通知订阅者
        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_FAILED,
            session_id=task.session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
            extra={"error": error},
        ))
        
        _rlog.warning(
            "task_manager",
            f"[TaskManager] 任务失败: {task_id} - {error}"
        )
    
    async def cancel_task(self, task_id: str, recursive: bool = True) -> None:
        """取消任务 (及其所有子任务)
        
        Args:
            task_id: 任务ID
            recursive: 是否递归取消所有子任务
            
        Raises:
            ValueError: 任务不存在
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        
        # 标记当前任务为已取消
        task.status = TaskStatus.CANCELLED
        task.completed_at = datetime.now()
        
        # 递归取消子任务
        if recursive:
            for child_id in task.children_ids:
                try:
                    await self.cancel_task(child_id, recursive=True)
                except ValueError:
                    pass  # 子任务可能已被删除
        
        # 通知订阅者
        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_CANCELLED,
            session_id=task.session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
        ))
        
        _rlog.debug(
            "task_manager",
            f"[TaskManager] 任务取消: {task_id} - {task.name}"
        )

    # ─────────────────────────────────────────────────────────
    # 摘要（供仪表盘 tooltip 使用，机械生成、不调 LLM）
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _compute_digest(task: Task) -> Dict[str, Any]:
        """根据任务的元数据机械地生成一段简短摘要。

        - 不进行任何网络/LLM 调用，O(1) 时间内完成。
        - 输出体积保持在 ~200 字节内，确保事件流足够小。
        """
        digest: Dict[str, Any] = {
            "type": task.task_type.value,
            "status": task.status.value,
        }

        # 时长（毫秒）
        if task.started_at and task.completed_at:
            delta_ms = int(
                (task.completed_at - task.started_at).total_seconds() * 1000
            )
            digest["duration_ms"] = delta_ms

        # 错误片段
        if task.error:
            digest["error"] = task.error[:160]

        meta = task.metadata or {}

        # tool_execution: tool_name, args 概要, result 长度
        if "tool_name" in meta:
            digest["tool"] = str(meta["tool_name"])[:60]
        if "tool_args_preview" in meta:
            digest["args_preview"] = str(meta["tool_args_preview"])[:120]
        if "result_size" in meta:
            digest["result_size"] = meta["result_size"]

        # llm_call: model + token usage
        if "model" in meta:
            digest["model"] = str(meta["model"])[:60]
        if "usage" in meta and isinstance(meta["usage"], dict):
            usage = meta["usage"]
            digest["tokens"] = {
                "in": usage.get("input") or usage.get("prompt_tokens"),
                "out": usage.get("output") or usage.get("completion_tokens"),
            }

        return digest

    # ─────────────────────────────────────────────────────────
    # 日志事件
    # ─────────────────────────────────────────────────────────

    async def emit_log_event(
        self,
        task_id: str,
        level: str,
        message: str,
    ) -> None:
        """发送任务日志事件

        Args:
            task_id: 任务ID
            level: 日志级别 (info/warning/error/message)
            message: 日志消息
        """
        task = self._tasks.get(task_id)
        if task is None:
            return

        await self._notify_subscribers(TaskEvent(
            event_type=EventType.TASK_LOG,
            session_id=task.session_id,
            task_id=task_id,
            timestamp=datetime.now(),
            task_snapshot=task.to_dict(),
            extra={"log_level": level, "log_message": message},
        ))

    # ─────────────────────────────────────────────────────────
    # 订阅者管理
    # ─────────────────────────────────────────────────────────
    
    def subscribe(self, subscriber: TaskSubscriber) -> None:
        """注册订阅者
        
        Args:
            subscriber: 订阅者对象
        """
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)
            _rlog.info(
                "task_manager",
                f"[TaskManager] 订阅者注册: {subscriber.get_subscriber_id()}"
            )
    
    def unsubscribe(self, subscriber_id: str) -> None:
        """注销订阅者
        
        Args:
            subscriber_id: 订阅者ID
        """
        self._subscribers = [
            s for s in self._subscribers
            if s.get_subscriber_id() != subscriber_id
        ]
        _rlog.info(
            "task_manager",
            f"[TaskManager] 订阅者注销: {subscriber_id}"
        )
    
    async def _notify_subscribers(self, event: TaskEvent) -> None:
        """异步通知所有订阅者（并发执行）
        
        Args:
            event: 任务事件
        """
        tasks = []
        
        for subscriber in self._subscribers:
            # 事件过滤
            subscribed_events = subscriber.get_subscribed_events()
            if subscribed_events and event.event_type not in subscribed_events:
                continue
            
            # 并发调用所有订阅者
            tasks.append(self._safe_notify(subscriber, event))
        
        # 并发执行所有通知（不等待单个订阅者完成）
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _safe_notify(self, subscriber: TaskSubscriber, event: TaskEvent) -> None:
        """安全地通知单个订阅者，捕获异常
        
        Args:
            subscriber: 订阅者对象
            event: 任务事件
        """
        try:
            await subscriber.on_event(event)
        except Exception as e:
            _rlog.error(
                "task_manager",
                f"[TaskManager] 订阅者处理事件失败: {subscriber.get_subscriber_id()} - {e}"
            )
    
    # ─────────────────────────────────────────────────────────
    # 状态导出
    # ─────────────────────────────────────────────────────────
    
    def export_session_tasks(
        self,
        session_id: str,
        include_completed: bool = False,
    ) -> Dict[str, Any]:
        """导出指定 Session 的任务树
        
        Args:
            session_id: Session ID
            include_completed: 是否包含已完成的任务
            
        Returns:
            任务树字典
        """
        task_ids = self._session_tasks.get(session_id, [])
        
        # 获取根任务 (没有父任务的任务)
        root_tasks = []
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                continue
            
            # 过滤已完成任务
            if not include_completed and task.is_finished():
                continue
            
            # 只导出根任务 (子任务通过递归包含)
            if task.parent_id is None:
                root_tasks.append(task)
        
        return {
            "session_id": session_id,
            "task_count": len(task_ids),
            "root_tasks": [self._export_task_tree(task) for task in root_tasks],
        }
    
    def _export_task_tree(self, task: Task) -> Dict[str, Any]:
        """递归导出任务树
        
        Args:
            task: 任务对象
            
        Returns:
            任务树字典
        """
        task_dict = task.to_dict()
        
        # 添加子任务 (递归)
        if task.children:
            task_dict["children"] = [
                self._export_task_tree(child)
                for child in task.children
            ]
        
        return task_dict
    
    def export_all_tasks(self) -> Dict[str, Any]:
        """导出所有 Session 的任务状态
        
        Returns:
            所有任务字典
        """
        sessions = {}
        for session_id in self._session_tasks.keys():
            sessions[session_id] = self.export_session_tasks(session_id)
        
        return {
            "total_sessions": len(self._session_tasks),
            "total_tasks": len(self._tasks),
            "sessions": sessions,
        }
    
    def get_active_sessions(self) -> List[str]:
        """获取有活跃任务的 Session 列表
        
        Returns:
            Session ID 列表
        """
        active_sessions = []
        for session_id, task_ids in self._session_tasks.items():
            # 检查是否有未完成的任务
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task and not task.is_finished():
                    active_sessions.append(session_id)
                    break
        
        return active_sessions
    
    def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务对象
        
        Args:
            task_id: 任务ID
            
        Returns:
            任务对象,如果不存在则返回 None
        """
        return self._tasks.get(task_id)
    
    # ─────────────────────────────────────────────────────────
    # 内部辅助方法
    # ─────────────────────────────────────────────────────────
    
    def _check_and_cleanup_session(self, session_id: str) -> None:
        """检查并清理 Session 的任务 (如果超过限制)
        
        Args:
            session_id: Session ID
        """
        task_ids = self._session_tasks.get(session_id, [])
        
        if len(task_ids) <= self._max_memory_tasks_per_session:
            return
        
        # 找出已完成的任务 (最老的先删除)
        finished_tasks = []
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task and task.is_finished():
                finished_tasks.append((task.completed_at, task_id))
        
        # 按完成时间排序,删除最老的
        finished_tasks.sort()
        tasks_to_remove = len(task_ids) - self._max_memory_tasks_per_session
        
        for i in range(min(tasks_to_remove, len(finished_tasks))):
            _, task_id = finished_tasks[i]
            self._remove_task(task_id)
            
        _rlog.info(
            "task_manager",
            f"[TaskManager] Session {session_id} 任务清理: 移除 {min(tasks_to_remove, len(finished_tasks))} 个已完成任务"
        )
    
    def _remove_task(self, task_id: str) -> None:
        """移除任务 (内部方法)
        
        Args:
            task_id: 任务ID
        """
        task = self._tasks.get(task_id)
        if task is None:
            return
        
        # 从 Session 任务列表移除
        session_id = task.session_id
        if session_id in self._session_tasks:
            self._session_tasks[session_id] = [
                tid for tid in self._session_tasks[session_id]
                if tid != task_id
            ]
        
        # 从父任务移除
        if task.parent:
            task.parent.remove_child(task_id)
        
        # 删除任务
        del self._tasks[task_id]
