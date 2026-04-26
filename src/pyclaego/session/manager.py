"""Session 管理器 - 管理所有会话"""

import asyncio
from pathlib import Path
from typing import Dict, Optional, List, Callable, Awaitable, Any

from .session import Session, generate_session_id, validate_session_id
from ..config import get_config
from ..logging import get_running_log
from ..task_manager import (
    TaskManager,
    TaskType,
    SessionTaskHandlerV2,
    TextSubscriber,
)

_rlog = get_running_log()


class SessionManager:
    """Session 管理器
    
    功能：
    - 创建和管理 Session
    - 从工作空间加载已存在的 Session
    - 路由消息到对应的 Session
    """
    
    def __init__(self, workspace_root: str = "./workspaces"):
        """初始化 SessionManager
        
        Args:
            workspace_root: 工作空间根目录
        """
        self.workspace_root = Path(workspace_root)
        self.sessions: Dict[str, Session] = {}

        # 【2026年04月22日新增】unsolicited 广播回调（由 CoreScheduler 注入）
        # 调用时应传入含 session_id 字段的 message dict。
        self._broadcast_fn: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        
        # 确保工作空间根目录存在
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        
        # 【2026年04月10日新增】初始化 TextSubscriber，将任务树实时导出到文本文件
        task_output_dir = self.workspace_root.parent / ".cache" / "task_output"
        task_output_dir.mkdir(parents=True, exist_ok=True)
        
        self._text_subscriber = TextSubscriber(
            output_dir=task_output_dir,
            subscriber_id="session_manager_text_subscriber",
            auto_start=True,
        )
        
        # 注册订阅者到 TaskManager
        task_manager = TaskManager.get_instance()
        task_manager.subscribe(self._text_subscriber)
        
        _rlog.info(
            "core_service",
            f"[SessionManager] TextSubscriber 已启动，输出目录: {task_output_dir}"
        )
        
        # 【2026年04月11日新增】初始化任务桥接服务器，将任务事件推送到 Web Server
        from ..web.task_bridge import TaskBridgeServer
        
        config = get_config()
        web_config = config.get("web", {})
        task_bridge_config = web_config.get("task_bridge", {})
        
        bridge_host = task_bridge_config.get("host", "127.0.0.1")
        bridge_port = task_bridge_config.get("port", 18766)
        
        self._task_bridge = TaskBridgeServer(
            host=bridge_host,
            port=bridge_port,
            subscriber_id="session_manager_task_bridge"
        )
        
        # 注册桥接服务器到 TaskManager
        task_manager.subscribe(self._task_bridge)
        
        # 启动桥接服务器（异步）
        loop = asyncio.get_event_loop()
        loop.create_task(self._task_bridge.start())
        
        _rlog.info(
            "core_service",
            f"[SessionManager] TaskBridgeServer 已启动: ws://{bridge_host}:{bridge_port}"
        )
    
    async def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        user_id: str = "default_user",
        agent_config: Optional[Dict] = None
    ) -> Session:
        """获取或创建 Session
        
        Args:
            session_id: Session ID（如果为 None 则生成新ID）
            user_id: 用户ID
            agent_config: Agent 配置（用于创建新 Session）
            
        Returns:
            Session 实例
            
        Raises:
            ValueError: session_id 格式不合法
        """
        # 如果没有提供 session_id，生成新的
        if session_id is None:
            session_id = generate_session_id()
            _rlog.info("core_service", f"[SessionManager] 生成新 Session ID: {session_id}")
        else:
            # 验证用户提供的 session_id 格式
            if not validate_session_id(session_id):
                raise ValueError(
                    f"Invalid session_id format: '{session_id}'. "
                    "Session ID must contain only lowercase letters, digits, and underscores."
                )
        
        # 检查是否已存在(内存缓存)
        if session_id in self.sessions:
            session = self.sessions[session_id]
            _rlog.info("core_service", f"[SessionManager] 找到已缓存的 Session: {session_id}")
            return session
        
        # 创建新 Session (Session.__init__会再次验证格式并加载/创建目录)
        session = Session(
            session_id=session_id,
            user_id=user_id,
            workspace_root=str(self.workspace_root),
        )
        # 【2026年04月22日新增】注入 broadcast handler，供 cron 等主动消息推送
        if self._broadcast_fn is not None:
            session.set_broadcast_handler(self._broadcast_fn)
        self.sessions[session_id] = session
        _rlog.info("core_service", f"[SessionManager] 创建新 Session: {session_id}, 工作空间: {session.workspace_path}")
        
        return session
    
    def get_session(self, session_id: str) -> Optional[Session]:
        """获取指定 Session
        
        Args:
            session_id: Session ID
            
        Returns:
            Session 实例，如果不存在则返回 None
        """
        return self.sessions.get(session_id)
    
    def list_sessions(self, user_id: Optional[str] = None) -> List[Dict]:
        """列出 Session
        
        Args:
            user_id: 用户ID（可选，用于过滤）
            
        Returns:
            Session 信息列表
        """
        sessions = []
        for session in self.sessions.values():
            if user_id is None or session.user_id == user_id:
                sessions.append(session.get_info())
        return sessions
    
    async def subscribe_session(self, session_id: str) -> bool:
        """订阅 Session
        
        Args:
            session_id: Session ID
            
        Returns:
            是否成功
        """
        session = self.get_session(session_id)
        if session:
            session.subscribe()
            return True
        return False
    
    async def unsubscribe_session(self, session_id: str) -> bool:
        """取消订阅 Session
        
        Args:
            session_id: Session ID
            
        Returns:
            是否成功
        """
        session = self.get_session(session_id)
        if session:
            session.unsubscribe()
            return True
        return False
    
    async def route_message(
        self,
        session_id: str,
        message: Dict,
        user_id: str = "default_user",
        msg_update_handler: Optional[Callable] = None  # 【2026年03月30日19:04:15新增】回调参数
    ) -> Optional[Dict]:
        """路由消息到指定 Session
        
        集成 TaskManager:
        1. 创建顶层任务 (USER_MESSAGE)
        2. 包装 msg_update_handler (SessionTaskHandler)
        3. 传递给 Session 处理
        4. 任务完成/失败标记
        
        Args:
            session_id: Session ID
            message: 消息内容
            user_id: 发送消息的用户ID（可选）
            msg_update_handler: 进度更新回调（可选）
            
        Returns:
            Session 的响应，如果 Session 不存在则返回 None
        """
        session = self.get_session(session_id)
        if session is None:
            _rlog.warning("core_service", f"[SessionManager] Session {session_id} 不存在")
            return None
        
        # ── 1. 创建新任务 ──────────────────────────────────────────
        task_manager = TaskManager.get_instance()
        
        # 获取消息内容作为任务名称
        content = message.get("content", "")
        task_name = f"User Message: {content[:50]}..." if len(content) > 50 else f"User Message: {content}"
        
        task_id = await task_manager.create_task(
            session_id=session_id,
            task_type=TaskType.USER_MESSAGE,
            name=task_name,
            parent_id=None,  # 顶层任务
            description="用户消息处理",
            user_id=user_id,
            message_type=message.get("type"),
        )
        
        _rlog.debug(
            "core_service",
            f"[SessionManager] 创建任务: {task_id} for session {session_id}"
        )
        
        # ── 2. 创建包装 handler ────────────────────────────────────
        wrapped_handler = SessionTaskHandlerV2(
            session_id=session_id,
            user_id=user_id,
            task_id=task_id,
            original_handler=msg_update_handler,
        )
        
        # ── 3. 调用 Session 处理消息 ───────────────────────────────
        try:
            result = await session.process_message(
                message,
                user_id=user_id,
                msg_update_handler=wrapped_handler,  # 传递包装后的 handler
            )
            
            # ── 4. 任务结束,标记完成 ─────────────────────────────────
            await task_manager.complete_task(task_id, result=result)
            
            _rlog.debug(
                "core_service",
                f"[SessionManager] 任务完成: {task_id}"
            )
            
            return result
            
        except Exception as e:
            # ── 5. 任务失败 ──────────────────────────────────────────
            await task_manager.fail_task(task_id, str(e))
            
            _rlog.error(
                "core_service",
                f"[SessionManager] 任务失败: {task_id} - {e}"
            )
            
            raise
    
    def get_stats(self) -> Dict:
        """获取统计信息
        
        Returns:
            统计信息字典
        """
        total_sessions = len(self.sessions)
        active_sessions = sum(1 for s in self.sessions.values() if s.is_subscribed)
        
        return {
            "total_sessions": total_sessions,
            "active_sessions": active_sessions,
            "workspace_root": str(self.workspace_root)
        }

    # ──────────────────────────────────────────────────────────────────
    # 【2026年04月22日新增】广播回调与全局关闭
    # ──────────────────────────────────────────────────────────────────

    def set_broadcast_fn(
        self,
        broadcast_fn: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    ) -> None:
        """注入全局 unsolicited 广播回调 (供 CoreScheduler 调用)

        后续创建的所有 Session 都会拿到该回调; 已存在的 Session 也被补设。
        """
        self._broadcast_fn = broadcast_fn
        for s in self.sessions.values():
            s.set_broadcast_handler(broadcast_fn)

    async def shutdown_all(self) -> None:
        """关闭所有 Session（程序退出时调用）"""
        if not self.sessions:
            return
        _rlog.info(
            "core_service",
            f"[SessionManager] 开始关闭 {len(self.sessions)} 个 Session...",
        )
        results = await asyncio.gather(
            *(s.shutdown() for s in self.sessions.values()),
            return_exceptions=True,
        )
        for s, r in zip(list(self.sessions.values()), results):
            if isinstance(r, Exception):
                _rlog.warning(
                    "core_service",
                    f"[SessionManager] Session {s.session_id} 关闭异常: {r}",
                )
        _rlog.info("core_service", "[SessionManager] 所有 Session 已关闭")
