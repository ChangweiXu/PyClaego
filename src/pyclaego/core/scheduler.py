"""核心调度器模块 - 集成 Session 管理"""

import asyncio
import json
import traceback
import websockets
from typing import Dict, Any, Set, Optional, Callable
from datetime import datetime
from ..logging import get_running_log

_rlog = get_running_log()


class CoreScheduler:
    """中心化调度器 - 集成 Session 管理
    
    功能：
    - 作为 WebSocket 服务器独立运行
    - 管理 Session 生命周期
    - 路由消息到对应的 Session
    - 处理权限验证
    """
    
    def __init__(
        self, 
        host: str = "127.0.0.1", 
        port: int = 8765,
        workspace_root: str = "./workspaces"
    ):
        self.host = host
        self.port = port
        self.workspace_root = workspace_root
        self.running = False
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        
        # 客户端到 Session 的映射
        self.client_sessions: Dict[int, str] = {}  # client_id -> session_id
        
        # Session 到订阅者（客户端）的映射 - 用于广播
        self.session_subscribers: Dict[str, Set[int]] = {}  # session_id -> {client_ids}
        
        # 客户端 ID 到 WebSocket 的映射 - 用于广播时查找连接
        self.client_websockets: Dict[int, websockets.WebSocketServerProtocol] = {}  # client_id -> websocket
        
        # Session 管理器（延迟初始化）
        self.session_manager = None
        
    async def start(self) -> None:
        """启动核心调度器和 WebSocket 服务器"""
        self.running = True
        
        # 导入并初始化 SessionManager
        from pyclaego.session import SessionManager
        self.session_manager = SessionManager(workspace_root=self.workspace_root)

        # 【2026年04月22日新增】注入 unsolicited 广播回调 (cron 响应会走这里)
        self.session_manager.set_broadcast_fn(self._broadcast_unsolicited)
        
        timestamp = self._get_timestamp()
        _rlog.info("core_service", f"[CoreScheduler] PyClaw-CC Core Server (Session Mode) 启动")
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 核心调度器已启动")
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] Session 管理器已初始化")
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 工作空间: {self.workspace_root}")
        
        # 显示已加载的 Session
        stats = self.session_manager.get_stats()
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 已加载 {stats['total_sessions']} 个 Session")
        
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] WebSocket 服务器启动于 ws://{self.host}:{self.port}")
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 等待客户端连接...")
        print(f"[CoreScheduler] Core Server started at ws://{self.host}:{self.port}")
        
        # 启动 WebSocket 服务器（max_size=20MB，支持图片等大消息）
        async with websockets.serve(self.handle_client, self.host, self.port, max_size=20 * 1024 * 1024):
            # 保持服务器运行
            await asyncio.Future()  # 永远运行
        
    async def stop(self) -> None:
        """停止核心调度器"""
        self.running = False
        # 【2026年04月22日新增】退出前关闭所有 Session（停止 cron + 清理队列）
        if self.session_manager is not None:
            try:
                await self.session_manager.shutdown_all()
            except Exception as e:
                _rlog.error(
                    "core_service",
                    f"[CoreScheduler] shutdown_all 异常: {e}\n{traceback.format_exc()}",
                )
        _rlog.info("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 核心调度器已停止")
        
    async def handle_client(self, websocket: websockets.WebSocketServerProtocol) -> None:
        """处理客户端连接"""
        # 【2026年04月05日00:39:50修改】websocket 处理逻辑从串行改为 fire-and-forget，保证了后续命令可以即时处理（如 /stop）
        client_id = id(websocket)
        session_id = None

        # 注册客户端
        self.clients.add(websocket)
        self.client_websockets[client_id] = websocket
        _rlog.info("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 客户端 #{client_id} 已连接，当前连接数: {len(self.clients)}")

        # 连接级别的进度回调：只读 session_id，不需要写它
        async def msg_update_handler(progress_msg: Dict[str, Any]) -> None:
            progress_msg["type"] = "progress_update"
            if session_id:
                progress_msg["session_id"] = session_id
            await self._broadcast_to_session(
                session_id=session_id,
                message=progress_msg,
                exclude_client_id=None
            )

        # 处理单条 user_message 并发回响应（在独立 task 中运行，不阻塞读取循环）
        async def handle_user_message_task(message: Dict[str, Any]) -> None:
            try:
                response = await self.handle_message(
                    message,
                    client_id,
                    websocket,
                    msg_update_handler=msg_update_handler
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                import traceback
                _rlog.error("core_service", f"[CoreScheduler] [{self._get_timestamp()}] user_message 处理异常: {e}\n{traceback.format_exc()}")
                return

            # 尝试直接发给发送者；连接已断开时仅记录日志，不中断后续广播
            try:
                await websocket.send(json.dumps(response))
            except websockets.exceptions.ConnectionClosed:
                _rlog.info("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 发送者已断开，跳过直接发送 (client #{client_id})")
            except Exception as e:
                _rlog.error("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 直接发送响应失败 (client #{client_id}): {e}")

            # 无论发送者是否在线，都广播给同 Session 其他订阅者
            if response.get("type") == "response":
                await self._broadcast_to_session(
                    session_id=session_id,
                    message=response,
                    exclude_client_id=client_id
                )

        try:
            async for message_str in websocket:
                message = json.loads(message_str)
                message_type = message.get("type", "unknown")

                if message_type == "user_message":
                    # fire-and-forget：立刻继续读取下一条消息（/stop 可即时到达）
                    asyncio.create_task(handle_user_message_task(message))
                else:
                    # join_session 等：同步处理，session_id 需要在此建立后才能处理后续消息
                    response = await self.handle_message(
                        message, client_id, websocket,
                        msg_update_handler=msg_update_handler
                    )
                    if response.get("type") == "session_joined":
                        session_id = response.get("session_id")
                    await websocket.send(json.dumps(response))

        except websockets.exceptions.ConnectionClosed:
            _rlog.info("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 客户端 #{client_id} 断开连接")
        except Exception as e:
            import traceback
            _rlog.error("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 错误: {e}\n{traceback.format_exc()}")
        finally:
            await self._handle_client_disconnect(client_id, session_id, websocket)
        
    async def handle_message(
        self, 
        message: Dict[str, Any], 
        client_id: int,
        websocket: websockets.WebSocketServerProtocol,
        msg_update_handler: Optional[Callable] = None  # 【2026年03月30日19:01:31新增】回调参数
    ) -> Dict[str, Any]:
        """处理来自客户端的消息
        
        Args:
            message: 消息字典
            client_id: 客户端ID
            websocket: WebSocket 连接
            
        Returns:
            响应字典
        """
        timestamp = self._get_timestamp()
        message_type = message.get("type", "unknown")
        
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] [Core] 收到消息 (客户端 #{client_id})。类型: {message_type}")
        
        # 处理加入 Session 请求
        if message_type == "join_session":
            return await self._handle_join_session(message, client_id)
        
        # 处理用户消息（需要先加入 Session）
        elif message_type == "user_message":
            return await self._handle_user_message(
                message, 
                client_id,
                msg_update_handler=msg_update_handler  # 【2026年03月30日19:02:04新增】传递回调
            )
        
        # 未知消息类型
        else:
            return {
                "type": "error",
                "content": f"未知的消息类型: {message_type}",
                "timestamp": timestamp
            }
    
    async def _handle_join_session(
        self, 
        message: Dict[str, Any], 
        client_id: int
    ) -> Dict[str, Any]:
        """处理加入 Session 请求
        
        Args:
            message: 消息内容，包含可选的 session_id
            client_id: 客户端ID
            
        Returns:
            响应消息
        """
        timestamp = self._get_timestamp()
        requested_session_id = message.get("session_id")
        user_id = message.get("user_id", "default_user")
        
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 请求 Session ID: {requested_session_id or '(生成新ID)'}, 用户 ID: {user_id}")
        
        # 权限验证（当前使用 pass）
        if not await self._check_permission(user_id, "join_session"):
            _rlog.warning("core_service", f"[CoreScheduler] [{timestamp}] 权限验证失败")
            return {
                "type": "error",
                "content": "权限验证失败",
                "timestamp": timestamp
            }
        
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 权限验证通过")
        
        # 获取或创建 Session
        session = await self.session_manager.get_or_create_session(
            session_id=requested_session_id,
            user_id=user_id
        )
        
        # 订阅 Session
        session.subscribe()
        
        # 返回成功响应
        response = {
            "type": "session_joined",
            "session_id": session.session_id,
            "workspace_path": str(session.workspace_path),
            "is_new": requested_session_id is None,
            "session_info": session.get_info(),
            "timestamp": timestamp
        }
        
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 客户端 #{client_id} 已加入 Session: {session.session_id}")
        
        # 将客户端加入到 session_subscribers 映射
        self.client_sessions[client_id] = session.session_id
        if session.session_id not in self.session_subscribers:
            self.session_subscribers[session.session_id] = set()
        self.session_subscribers[session.session_id].add(client_id)
        
        return response
    
    async def _handle_user_message(
        self, 
        message: Dict[str, Any], 
        client_id: int,
        msg_update_handler: Optional[Callable] = None  # 【2026年03月30日19:02:20新增】回调参数
    ) -> Dict[str, Any]:
        """处理用户消息
        
        Args:
            message: 消息内容
            client_id: 客户端ID
            
        Returns:
            响应消息
        """
        timestamp = self._get_timestamp()
        
        # 检查客户端是否已加入 Session
        session_id = self.client_sessions.get(client_id)
        if not session_id:
            return {
                "type": "error",
                "content": "请先加入 Session（发送 join_session 消息）",
                "timestamp": timestamp
            }
        
        content = message.get("content", "")
        user_id = message.get("user_id", "default_user")
        request_id = message.get("request_id", "")  # 透传 request_id（供客户端多路复用）
        _rlog.info("core_service", f"[CoreScheduler] [{timestamp}] 用户消息 session={session_id}, user={user_id}, request_id={request_id}, content={content[:80]}")
        
        # 路由消息到 Session，传递 user_id
        response = await self.session_manager.route_message(
            session_id, 
            message, 
            user_id=user_id,
            msg_update_handler=msg_update_handler  # 【2026年03月30日19:02:10新增】传递回调
        )
        
        if response is None:
            return {
                "type": "error",
                "content": f"Session {session_id} 不存在",
                "request_id": request_id,
                "timestamp": timestamp
            }
        
        # 将 request_id 透传到 response，供客户端按 request_id 路由响应
        if request_id:
            response["request_id"] = request_id
        
        return response
    
    async def _check_permission(self, user_id: str, action: str) -> bool:
        """检查权限（当前版本使用 pass）
        
        Args:
            user_id: 用户ID
            action: 操作类型
            
        Returns:
            是否有权限
        """
        # TODO: 实现真实的权限验证逻辑
        # 当前版本直接返回 True
        return True
    
    async def _broadcast_unsolicited(self, message: Dict[str, Any]) -> None:
        """主动消息（cron 等无客户端 await 的消息）广播入口。

        从 message['session_id'] 取目标 session，转发给该 session 的所有订阅者。
        无订阅者时静默丢弃（cron 已经写盘）。
        """
        session_id = message.get("session_id")
        if not session_id:
            _rlog.warning(
                "core_service",
                "[CoreScheduler] unsolicited 广播缺少 session_id，已忽略",
            )
            return
        await self._broadcast_to_session(
            session_id=session_id,
            message=message,
            exclude_client_id=None,
        )

    async def _broadcast_to_session(
        self,
        session_id: str,
        message: Dict[str, Any],
        exclude_client_id: Optional[int] = None
    ) -> None:
        """广播消息给 Session 的所有订阅者
        
        Args:
            session_id: Session ID
            message: 要广播的消息
            exclude_client_id: 排除的客户端ID（通常是消息发送者）
        """
        if not session_id or session_id not in self.session_subscribers:
            return
        
        subscribers = self.session_subscribers[session_id]
        message_str = json.dumps(message)
        
        # 广播给所有订阅者（除了排除的客户端）
        broadcast_count = 0
        for client_id in subscribers:
            if client_id == exclude_client_id:
                continue
            
            websocket = self.client_websockets.get(client_id)
            if websocket and websocket in self.clients:
                try:
                    await websocket.send(message_str)
                    broadcast_count += 1
                except Exception as e:
                    _rlog.error("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 广播失败 (客户端 #{client_id}): {e}")
        
        if broadcast_count > 0:
            _rlog.info("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 已广播给 {broadcast_count} 个订阅者")
    
    async def _handle_client_disconnect(
        self,
        client_id: int,
        session_id: Optional[str],
        websocket: websockets.WebSocketServerProtocol
    ) -> None:
        """处理客户端断开连接
        
        Args:
            client_id: 客户端ID
            session_id: 客户端所在的 Session ID（如果有）
            websocket: WebSocket 连接
        """
        # 从 clients 集合中移除
        self.clients.discard(websocket)
        
        # 从 client_websockets 映射中移除
        self.client_websockets.pop(client_id, None)
        
        # 如果客户端订阅了 Session
        if session_id:
            # 从 session_subscribers 中移除
            if session_id in self.session_subscribers:
                self.session_subscribers[session_id].discard(client_id)
                # 如果 Session 没有订阅者了，清理空集合
                if not self.session_subscribers[session_id]:
                    del self.session_subscribers[session_id]
            
            # 从 client_sessions 中移除
            self.client_sessions.pop(client_id, None)
            
            # 取消 Session 订阅
            await self.session_manager.unsubscribe_session(session_id)
        
        _rlog.info("core_service", f"[CoreScheduler] [{self._get_timestamp()}] 客户端断开后当前连接数: {len(self.clients)}")
        
    def _get_timestamp(self) -> str:
        """获取当前时间戳"""
        return datetime.now().strftime("%H:%M:%S")


async def main():
    """主函数"""
    scheduler = CoreScheduler(host="127.0.0.1", port=8765, workspace_root="./workspaces")
    
    try:
        await scheduler.start()
    except KeyboardInterrupt:
        _rlog.info("core_service", "[scheduler.py] 用户中断，Core 服务器退出")
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
