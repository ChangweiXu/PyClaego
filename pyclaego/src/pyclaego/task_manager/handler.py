"""SessionTaskHandler - Session 级别的任务更新 Handler

包含:
- SessionTaskHandler: 包装 msg_update_handler 的任务更新拦截器
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..logging import get_running_log
from .base import TaskStatus, TaskType
from .manager import TaskManager

_rlog = get_running_log()


class SessionTaskHandler:
    """Session 级别的任务更新 Handler
    
    由 SessionManager.route_message() 创建,绑定 session_id 和 task_id。
    拦截 msg_update_handler 回调中的 "task" 字段并更新 TaskManager。
    
    使用示例:
        >>> task_manager = TaskManager.get_instance()
        >>> task_id = await task_manager.create_task(...)
        >>> 
        >>> handler = SessionTaskHandler(
        ...     session_id="sess_001",
        ...     task_id=task_id,
        ...     original_handler=ws_callback,
        ... )
        >>> 
        >>> # 传递给 Session
        >>> await session.process_message(
        ...     message={"content": "帮我分析代码"},
        ...     msg_update_handler=handler,
        ... )
    
    协议格式:
        msg_update_handler 接收的消息中可以包含 "task" 字段:
        
        {
            "type": "progress_update",
            "task": {
                "action": "start" | "progress" | "complete" | "fail",
                "task_type": "agent_loop" | "tool_execution" | ...,
                "name": "任务名称",
                "progress": 0.5,  # 可选,仅 progress 时
                "message": "进度描述",  # 可选
                "error": "错误信息",  # 可选,仅 fail 时
                "metadata": {...},  # 可选
            }
        }
    """
    
    def __init__(
        self,
        session_id: str,
        task_id: str,
        original_handler: Callable | None = None,
        depth: int = 0,  # 任务深度，根任务为 0，子任务递增
    ):
        """初始化 SessionTaskHandler
        
        Args:
            session_id: Session ID
            task_id: 任务ID
            original_handler: 原始的 msg_update_handler (可选)
        """
        self._session_id = session_id
        self._task_id = task_id
        self._original_handler = original_handler
        self._task_manager = TaskManager.get_instance()
        self._depth = depth

    def get_task_id(self) -> str:
        """获取当前任务 ID"""
        return self._task_id

    async def __call__(self, msg: dict[str, Any]) -> SessionTaskHandler | None:
        raise NotImplementedError("SessionTaskHandler is deprecated. Use SessionTaskHandlerV2 instead.")
    
    async def _handle_task_update(self, task_data: dict[str, Any]) -> SessionTaskHandler | None:
        raise NotImplementedError("SessionTaskHandler._handle_task_update() is not implemented.")

    async def start_subtask(
        self,
        task_type: str,
        name: str,
        description: str = "",
        metadata: dict | None = None,
        action: str = "pending"
    ) -> SessionTaskHandler:
        """创建并启动子任务
        
        Args:
            task_type: 任务类型 (字符串,如 "agent_loop")
            name: 任务名称
            description: 任务描述
            metadata: 元数据
            
        Returns:
            子任务的 SessionTaskHandler
            
        Raises:
            ValueError: 深度超限
        """
        # 检查深度限制
        if self._depth >= 10:
            raise ValueError("Task depth exceeds maximum (10)")
        
        # 转换 task_type 字符串为枚举
        try:
            task_type_enum = TaskType[task_type.upper()]
        except KeyError:
            raise ValueError(f"Unknown task type: {task_type}")
        
        # 创建子任务
        child_task_id = await self._task_manager.create_task(
            session_id=self._session_id,
            task_type=task_type_enum,
            name=name,
            description=description,
            parent_id=self._task_id,  # ← 当前任务作为父任务
            metadata=metadata or {},
        )
        
        # 立即启动子任务
        if action == "start":
            await self._task_manager.start_task(child_task_id)
        
        _rlog.debug(
            f"session_{self._session_id}",
            f"[SessionTaskHandler] 创建子任务: {child_task_id} (depth={self._depth + 1})"
        )
        
        # 返回子任务的 handler
        return SessionTaskHandler(
            session_id=self._session_id,
            task_id=child_task_id,
            original_handler=self._original_handler,  # 继承原始 handler
            depth=self._depth + 1,
        )


class SessionTaskHandlerV2(SessionTaskHandler):
    """Session 级别的任务更新 Handler V2
    
    相比 SessionTaskHandler，V2 版本：
    1. 废弃了 __call__() 回调接口
    2. 提供显式的任务生命周期方法（start/complete/fail/cancel）
    3. 提供结构化的日志方法（log_info/log_warning/log_error）
    4. 支持消息更新通知（msg_update）
    
    使用示例:
        >>> task_handler = SessionTaskHandlerV2(
        ...     session_id="sess_001",
        ...     user_id="user_123",
        ...     task_id=root_task_id,
        ... )
        >>> 
        >>> await task_handler.log_info("开始处理任务")
        >>> 
        >>> # 创建子任务
        >>> loop_handler = await task_handler.create_subtask(
        ...     task_type=TaskType.AGENT_LOOP,
        ...     name="Agent Loop #1",
        ... )
        >>> await loop_handler.start()
        >>> # ... 执行任务逻辑 ...
        >>> await loop_handler.complete()
    """
    
    def __init__(
        self,
        session_id: str,
        user_id: str,
        task_id: str,
        original_handler: Callable | None = None,
        depth: int = 0,  # 任务深度，根任务为 0，子任务递增
    ):
        super().__init__(
            session_id=session_id,
            task_id=task_id,
            original_handler=original_handler,
            depth=depth,
        )
        self._user_id = user_id
        self._subagent_id: str | None = None

    def get_session_id(self):
        """获取会话 ID"""
        return self._session_id

    def get_user_id(self):
        """获取用户 ID"""
        return self._user_id

    def set_subagent_id(self, subagent_id: str) -> None:
        """设置子代理 ID"""
        self._subagent_id = subagent_id

    def get_subagent_id(self) -> str | None:
        """获取子代理 ID"""
        return self._subagent_id

    async def __call__(self, msg: dict[str, Any]) -> None:
        """废弃的回调接口，不应再使用"""
        raise RuntimeError(
            "SessionTaskHandlerV2.__call__() is deprecated. "
            "Use explicit methods instead: log_info(), start(), complete(), etc."
        )

    # ------------------------------------------------------------------
    # 日志方法
    # ------------------------------------------------------------------

    async def msg_update(self, msg: str) -> None:
        """发送消息更新通知
        
        Args:
            msg: 消息内容
        """
        if self._original_handler:
            try:
                await self._original_handler({
                    "type": "message_update",
                    "content": msg,
                    "task_id": self._task_id,
                })
            except Exception as e:
                _rlog.error(
                    f"session_{self._session_id}",
                    f"[SessionTaskHandlerV2] msg_update 调用失败: {e}"
                )

        # 同时发送任务日志事件（供 task dashboard 使用）
        try:
            await self._task_manager.emit_log_event(
                self._task_id, "message", msg
            )
        except Exception:
            pass

    async def log_info(self, msg: str) -> None:
        """记录信息日志
        
        Args:
            msg: 日志消息
        """
        _rlog.info(f"session_{self._session_id}", msg)
        
        # 同时发送消息更新通知
        if self._original_handler:
            try:
                await self._original_handler({
                    "type": "log",
                    "level": "info",
                    "content": msg,
                    "task_id": self._task_id,
                })
            except Exception:
                pass  # 静默失败，不影响主流程

        # 发送任务日志事件（供 task dashboard 使用）
        try:
            await self._task_manager.emit_log_event(
                self._task_id, "info", msg
            )
        except Exception:
            pass

    async def log_warning(self, msg: str) -> None:
        """记录警告日志
        
        Args:
            msg: 日志消息
        """
        _rlog.warning(f"session_{self._session_id}", msg)
        
        if self._original_handler:
            try:
                await self._original_handler({
                    "type": "log",
                    "level": "warning",
                    "content": msg,
                    "task_id": self._task_id,
                })
            except Exception:
                pass

        try:
            await self._task_manager.emit_log_event(
                self._task_id, "warning", msg
            )
        except Exception:
            pass

    async def log_error(self, msg: str) -> None:
        """记录错误日志
        
        Args:
            msg: 日志消息
        """
        _rlog.error(f"session_{self._session_id}", msg)
        
        if self._original_handler:
            try:
                await self._original_handler({
                    "type": "log",
                    "level": "error",
                    "content": msg,
                    "task_id": self._task_id,
                })
            except Exception:
                pass

        try:
            await self._task_manager.emit_log_event(
                self._task_id, "error", msg
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 任务状态管理方法
    # ------------------------------------------------------------------

    async def update_task_status(
        self,
        status: TaskStatus,
        result: dict = {},
        error: str = "Unknown error",
    ) -> None:
        """更新任务状态（不推荐直接使用，请使用 start/complete/fail/cancel）
        
        Args:
            status: 任务状态
            result: 任务结果（可选）
            error: 错误信息（可选）
        """
        if status == TaskStatus.RUNNING:
            await self.start()
        elif status == TaskStatus.COMPLETED:
            await self.complete(result=result)
        elif status == TaskStatus.FAILED:
            await self.fail(error=error)
        elif status == TaskStatus.CANCELLED:
            await self.cancel()
        else:
            _rlog.warning(
                f"session_{self._session_id}",
                f"[SessionTaskHandlerV2] 不支持的状态更新: {status}"
            )

    async def start(self) -> None:
        """启动任务"""
        try:
            await self._task_manager.start_task(self._task_id)
        except Exception as e:
            _rlog.error(
                f"session_{self._session_id}",
                f"[SessionTaskHandlerV2] 启动任务失败: {e}"
            )
            raise

    async def complete(
        self,
        result: dict = {},
    ) -> None:
        """完成任务
        
        Args:
            result: 任务结果（可选）
        """
        try:
            await self._task_manager.complete_task(
                self._task_id,
                result=result
            )
        except Exception as e:
            _rlog.error(
                f"session_{self._session_id}",
                f"[SessionTaskHandlerV2] 完成任务失败: {e}"
            )
            raise

    async def fail(self, error: str = "Unknown error") -> None:
        """标记任务失败
        
        Args:
            error: 错误信息
        """
        try:
            await self._task_manager.fail_task(
                self._task_id,
                error=error
            )
        except Exception as e:
            _rlog.error(
                f"session_{self._session_id}",
                f"[SessionTaskHandlerV2] 标记任务失败时出错: {e}"
            )
            raise

    async def cancel(self) -> None:
        """取消任务"""
        try:
            await self._task_manager.cancel_task(self._task_id)
        except Exception as e:
            _rlog.error(
                f"session_{self._session_id}",
                f"[SessionTaskHandlerV2] 取消任务失败: {e}"
            )
            raise

    # ------------------------------------------------------------------
    # 子任务管理
    # ------------------------------------------------------------------

    async def create_subtask(
        self,
        task_type: TaskType,
        name: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> SessionTaskHandlerV2:
        """创建并返回子任务的 handler
        
        Args:
            task_type: 任务类型
            name: 任务名称
            description: 任务描述
            metadata: 元数据
            
        Returns:
            子任务的 SessionTaskHandlerV2 实例
            
        Raises:
            ValueError: 深度超限或父任务不存在
        """
        # 检查深度限制
        if self._depth >= 10:
            raise ValueError("Task depth exceeds maximum (10)")
        
        # 创建子任务
        child_task_id = await self._task_manager.create_task(
            session_id=self._session_id,
            task_type=task_type,
            name=name,
            parent_id=self._task_id,
            description=description,
            **(metadata or {})
        )
        
        # 创建并返回子任务的 handler
        child_handler = SessionTaskHandlerV2(
            session_id=self._session_id,
            user_id=self._user_id,
            task_id=child_task_id,
            original_handler=self._original_handler,
            depth=self._depth + 1,
        )
        
        return child_handler

    async def create_sibling_task(
        self,
        task_type: TaskType,
        name: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> SessionTaskHandlerV2:
        """创建兄弟任务（与当前任务共享同一父任务），用于并行子任务场景。

        例如：在 agent loop 任务下创建一个并行的 memory_compress 任务，
        两者都挂在同一父节点下，互不阻塞。

        Args:
            task_type: 任务类型
            name: 任务名称
            description: 任务描述
            metadata: 元数据

        Returns:
            兄弟任务的 SessionTaskHandlerV2 实例

        Raises:
            ValueError: 深度超限
        """
        if self._depth >= 10:
            raise ValueError("Task depth exceeds maximum (10)")

        # 查找当前任务的父 task_id（兄弟任务共享同一父节点）
        current_task = self._task_manager.get_task(self._task_id)
        parent_id = current_task.parent_id if current_task else None

        sibling_task_id = await self._task_manager.create_task(
            session_id=self._session_id,
            task_type=task_type,
            name=name,
            parent_id=parent_id,
            description=description,
            **(metadata or {})
        )

        _rlog.debug(
            f"session_{self._session_id}",
            f"[SessionTaskHandlerV2] 创建兄弟任务: {sibling_task_id} "
            f"(parent={parent_id}, depth={self._depth})"
        )

        return SessionTaskHandlerV2(
            session_id=self._session_id,
            user_id=self._user_id,
            task_id=sibling_task_id,
            original_handler=self._original_handler,
            depth=self._depth,  # 同级，深度不变
        )


# ============================================================================
# PersonalSpace 模型 — WidgetTaskHandler
# ============================================================================
# 设计目标：
# - 在不破坏现存 SessionTaskHandlerV2 调用方的前提下，引入面向 PersonalSpace
#   + Widget 的新句柄类型。
# - 当前实现为 V2 的薄包装；后续 Cleanup 阶段会把 V2 的实现整体迁入
#   WidgetTaskHandler，并把 SessionTaskHandlerV2 退化为别名。
# ----------------------------------------------------------------------------

from .belonging import TaskBelonging  # noqa: E402  (put after class defs to avoid cycles)


class WidgetTaskHandler(SessionTaskHandlerV2):
    """PersonalSpace 模型下的任务句柄。

    与 ``SessionTaskHandlerV2`` 的关系：
    - 是其子类；在 V2 行为之上额外承载 ``belongs_to: TaskBelonging``
    - 重写 ``create_subtask`` / ``create_sibling_task``，确保派生句柄仍是
      ``WidgetTaskHandler`` 并继承 ``belongs_to``，避免在树枝向下时丢失归属
    - 提供 ``derive_for_subagent`` 做不可变派生

    构造方式：
    - ``WidgetTaskHandler.from_belonging(belonging, user_id, task_id, ...)``：推荐
    - 直接调用 ``WidgetTaskHandler(session_id=..., user_id=..., task_id=...)``：
      等同于 ``TaskBelonging(ps_id=session_id)``，与 V2 完全一致
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        task_id: str,
        original_handler: Callable | None = None,
        depth: int = 0,
        belongs_to: TaskBelonging | None = None,
    ):
        super().__init__(
            session_id=session_id,
            user_id=user_id,
            task_id=task_id,
            original_handler=original_handler,
            depth=depth,
        )
        if belongs_to is None:
            belongs_to = TaskBelonging(ps_id=session_id)
        self._belongs_to: TaskBelonging = belongs_to
        # 把 subagent_id 同步到 V2 字段，供旧逻辑读取
        if belongs_to.subagent_id:
            self._subagent_id = belongs_to.subagent_id

    # ---- new accessors ----
    @property
    def belongs_to(self) -> TaskBelonging:
        return self._belongs_to

    def get_belonging(self) -> TaskBelonging:
        return self._belongs_to

    @classmethod
    def from_belonging(
        cls,
        belonging: TaskBelonging,
        user_id: str,
        task_id: str,
        original_handler: Callable | None = None,
        depth: int = 0,
    ) -> WidgetTaskHandler:
        return cls(
            session_id=belonging.key(),
            user_id=user_id,
            task_id=task_id,
            original_handler=original_handler,
            depth=depth,
            belongs_to=belonging,
        )

    def derive_for_subagent(self, subagent_id: str) -> WidgetTaskHandler:
        """生成挂在同一任务节点上的子代理句柄（不可变派生）。

        派生出的句柄共享 ``task_id``、``user_id`` 与 ``original_handler``，
        仅 ``belongs_to`` 上多出 ``subagent_id``。常用于 SubAgent 在父
        widget 任务下做日志/工件归属。
        """
        new_belonging = self._belongs_to.with_subagent(subagent_id)
        return WidgetTaskHandler(
            session_id=new_belonging.key(),
            user_id=self._user_id,
            task_id=self._task_id,
            original_handler=self._original_handler,
            depth=self._depth,
            belongs_to=new_belonging,
        )

    # ------------------------------------------------------------------
    # 重写 V2 的子任务/兄弟任务工厂，使其返回 WidgetTaskHandler 并继承归属
    # ------------------------------------------------------------------

    async def create_subtask(
        self,
        task_type: TaskType,
        name: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> WidgetTaskHandler:
        """创建并返回子任务句柄（保留 belongs_to）。

        与 ``SessionTaskHandlerV2.create_subtask`` 的差别：
        - 返回 ``WidgetTaskHandler`` 而非 ``SessionTaskHandlerV2``
        - ``belongs_to`` 沿用父句柄（subagent_id 不继承到下层任务）
        """
        if self._depth >= 10:
            raise ValueError("Task depth exceeds maximum (10)")

        child_task_id = await self._task_manager.create_task(
            session_id=self._session_id,
            task_type=task_type,
            name=name,
            parent_id=self._task_id,
            description=description,
            **(metadata or {}),
        )

        return WidgetTaskHandler(
            session_id=self._session_id,
            user_id=self._user_id,
            task_id=child_task_id,
            original_handler=self._original_handler,
            depth=self._depth + 1,
            belongs_to=self._belongs_to,
        )

    async def create_sibling_task(
        self,
        task_type: TaskType,
        name: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> WidgetTaskHandler:
        """创建兄弟任务句柄（同一父节点 / 同 belongs_to）。"""
        if self._depth >= 10:
            raise ValueError("Task depth exceeds maximum (10)")

        current_task = self._task_manager.get_task(self._task_id)
        parent_id = current_task.parent_id if current_task else None

        sibling_task_id = await self._task_manager.create_task(
            session_id=self._session_id,
            task_type=task_type,
            name=name,
            parent_id=parent_id,
            description=description,
            **(metadata or {}),
        )

        _rlog.debug(
            f"session_{self._session_id}",
            f"[WidgetTaskHandler] 创建兄弟任务: {sibling_task_id} "
            f"(parent={parent_id}, depth={self._depth})",
        )

        return WidgetTaskHandler(
            session_id=self._session_id,
            user_id=self._user_id,
            task_id=sibling_task_id,
            original_handler=self._original_handler,
            depth=self._depth,
            belongs_to=self._belongs_to,
        )


# 向后兼容别名（迁移期保留）
# TaskHandler = WidgetTaskHandler
