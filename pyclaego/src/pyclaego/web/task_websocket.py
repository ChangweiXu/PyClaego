"""WebSocket 任务路由实现

通过内部 WebSocket 桥接接收 CoreScheduler 进程的任务事件。

架构：
    CoreScheduler 进程                     Web Server 进程（本文件）
    ┌─────────────────────┐                ┌──────────────────┐
    │ TaskBridgeServer    │    内部WebSocket   │  TaskBridgeClient│
    │   (18766端口)       │ ══════════════> │  (连接到18766)   │
    │                     │                │   │              │
    │ 推送任务事件         │                │   v              │
    └─────────────────────┘                │ 广播到浏览器      │
                                          └──────────────────┘
"""

import asyncio
import json
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_config
from ..logging import get_running_log

_rlog = get_running_log()
router = APIRouter()


class TaskBridgeClient:
    """任务事件桥接客户端（运行在 Web Server 进程）
    
    功能：
    - 连接到 CoreScheduler 进程的桥接服务器
    - 接收任务事件
    - 广播到所有浏览器 WebSocket 客户端
    """
    
    def __init__(
        self,
        bridge_host: str = "127.0.0.1",
        bridge_port: int = 18766
    ):
        self.bridge_url = f"ws://{bridge_host}:{bridge_port}"
        
        # 浏览器 WebSocket 连接池
        self._browser_clients: set[WebSocket] = set()
        
        # 桥接连接
        self._bridge_ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_task: asyncio.Task | None = None
        
        # 任务缓存（从桥接服务器接收）
        self._task_cache: dict[str, dict[str, Any]] = {}
        
        # 任务日志缓存 (task_id -> list of log entries)
        self._task_logs: dict[str, list] = {}
        
        _rlog.info("web_api", f"[TaskBridgeClient] 初始化，桥接地址: {self.bridge_url}")
    
    async def start(self) -> None:
        """启动客户端"""
        if self._running:
            return
        
        self._running = True
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        _rlog.info("web_api", "[TaskBridgeClient] 已启动")
    
    async def stop(self) -> None:
        """停止客户端"""
        _rlog.info("web_api", "[TaskBridgeClient] 正在停止...")
        self._running = False
        
        # 停止重连任务
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        
        # 关闭桥接连接
        if self._bridge_ws:
            await self._bridge_ws.close()
        
        # 关闭所有浏览器连接
        clients = self._browser_clients.copy()
        for client in clients:
            try:
                await client.close()
            except Exception:
                pass
        
        self._browser_clients.clear()
        _rlog.info("web_api", "[TaskBridgeClient] 已停止")
    
    async def _reconnect_loop(self) -> None:
        """自动重连循环"""
        while self._running:
            try:
                _rlog.info("web_api", f"[TaskBridgeClient] 正在连接桥接服务器: {self.bridge_url}")
                async with websockets.connect(self.bridge_url, max_size=2**23) as websocket:
                    self._bridge_ws = websocket
                    # 清空缓存，等待新的分块快照
                    self._task_cache.clear()
                    self._task_logs.clear()
                    _rlog.info("web_api", "[TaskBridgeClient] 已连接到桥接服务器，等待快照")
                    
                    # 接收消息并广播
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            await self._handle_bridge_message(data)
                        except Exception as e:
                            _rlog.error("web_api", f"[TaskBridgeClient] 处理消息失败: {e}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                _rlog.error("web_api", f"[TaskBridgeClient] 连接错误: {e}")
            
            # 断开后等待 3 秒重连
            if self._running:
                _rlog.info("web_api", "[TaskBridgeClient] 3秒后重新连接...")
                await asyncio.sleep(3)
    
    async def _handle_bridge_message(self, data: dict[str, Any]) -> None:
        """处理来自桥接服务器的消息
        
        消息类型：
        - snapshot_chunk: 分块快照（每个 session 一块，仅含 tasks），合并到本地缓存
        - snapshot_logs_chunk: 单个 task 的日志切片（按 LOGS_PER_CHUNK 条）
        - snapshot_done: 快照发送完成，构建任务树并广播到浏览器
        - task_update: 增量更新单个任务
        - task_log: 日志事件
        """
        msg_type = data.get("type")
        
        if msg_type == "task_log":
            # 日志事件：追加到本地缓存，直接转发到浏览器
            task_id = data.get("task_id", "")
            if task_id not in self._task_logs:
                self._task_logs[task_id] = []
            log_entry = {
                "timestamp": data.get("timestamp"),
                "level": data.get("log_level", "info"),
                "message": data.get("log_message", ""),
            }
            self._task_logs[task_id].append(log_entry)
            
            browser_message = {
                "type": "task_log",
                "task_id": task_id,
                "session_id": data.get("session_id", ""),
                "log": log_entry,
            }
            await self._broadcast_to_browsers(browser_message)
            return
        
        if msg_type == "snapshot_chunk":
            # 分块快照（仅 tasks）：合并到本地缓存，暂不广播
            session_id = data.get("session_id", "")
            self._task_cache[session_id] = data.get("tasks", {})
            _rlog.debug("web_api", f"[TaskBridgeClient] 收到快照分块: session={session_id}")
            return

        if msg_type == "snapshot_logs_chunk":
            # 单个 task 的日志切片：按 seq 顺序追加到本地缓存
            task_id = data.get("task_id", "")
            seq = data.get("seq", 0)
            logs = data.get("logs", [])
            if seq == 0:
                # 首片：重置该 task 的日志缓存，避免重连后重复累加
                self._task_logs[task_id] = []
            elif task_id not in self._task_logs:
                self._task_logs[task_id] = []
            self._task_logs[task_id].extend(logs)
            return

        if msg_type == "snapshot_done":
            # 快照接收完成：分块广播到浏览器（每个 session 一块）
            _rlog.info("web_api", f"[TaskBridgeClient] 快照接收完成，共 {len(self._task_cache)} 个 Session")
            for session_id, tasks in self._task_cache.items():
                chunk = {"type": "snapshot_chunk", "session_id": session_id, "tasks": tasks}
                await self._broadcast_to_browsers(chunk)
            done = {"type": "snapshot_done", "timestamp": data.get("timestamp")}
            await self._broadcast_to_browsers(done)
            return
        
        if msg_type == "task_update":
            # 增量更新：更新单个任务的快照，直接转发增量（O(1)，不重建任务树）
            session_id = data.get("session_id", "")
            task_id = data.get("task_id", "")
            task_snapshot = data.get("task_snapshot")
            
            if session_id not in self._task_cache:
                self._task_cache[session_id] = {}
            if task_snapshot is not None:
                self._task_cache[session_id][task_id] = task_snapshot
            
            browser_message = {
                "type": "task_update",
                "session_id": session_id,
                "task_id": task_id,
                "task_snapshot": task_snapshot,
            }
            await self._broadcast_to_browsers(browser_message)
            return
        
        _rlog.warning("web_api", f"[TaskBridgeClient] 未知消息类型: {msg_type}")
    
    async def _broadcast_to_browsers(self, message: dict[str, Any]) -> None:
        """广播消息到所有浏览器客户端"""
        if not self._browser_clients:
            return
        
        message_json = json.dumps(message, ensure_ascii=False)
        clients = self._browser_clients.copy()
        disconnected = []
        
        for client in clients:
            try:
                await client.send_text(message_json)
            except Exception as e:
                _rlog.warning("web_api", f"[TaskBridgeClient] 广播失败，将清理客户端: {e}")
                disconnected.append(client)
        
        # 清理断开的客户端
        for client in disconnected:
            self._browser_clients.discard(client)
    
    async def add_browser_connection(self, websocket: WebSocket) -> None:
        """添加浏览器连接，发送分块快照"""
        self._browser_clients.add(websocket)
        _rlog.info("web_api", f"[TaskBridgeClient] 浏览器客户端已添加，当前连接数: {len(self._browser_clients)}")
        
        # 发送分块快照：每个 session 一块，最后发送 snapshot_done
        try:
            for session_id, tasks in self._task_cache.items():
                chunk = {"type": "snapshot_chunk", "session_id": session_id, "tasks": tasks}
                await websocket.send_text(json.dumps(chunk, ensure_ascii=False))
            done = {"type": "snapshot_done", "timestamp": asyncio.get_event_loop().time()}
            await websocket.send_text(json.dumps(done, ensure_ascii=False))
            _rlog.info("web_api", f"[TaskBridgeClient] 已发送分块快照到浏览器，共 {len(self._task_cache)} 个 Session")
        except Exception as e:
            _rlog.error("web_api", f"[TaskBridgeClient] 发送初始快照失败: {e}")
            self._browser_clients.discard(websocket)
    
    async def remove_browser_connection(self, websocket: WebSocket) -> None:
        """移除浏览器连接"""
        self._browser_clients.discard(websocket)
        _rlog.info("web_api", f"[TaskBridgeClient] 浏览器客户端已移除，当前连接数: {len(self._browser_clients)}")


# 全局桥接客户端实例
_bridge_client: TaskBridgeClient | None = None


def get_bridge_client() -> TaskBridgeClient:
    """获取或创建桥接客户端单例"""
    global _bridge_client
    
    if _bridge_client is None:
        # 读取配置
        config = get_config()
        web_config = config.get("web", {})
        task_bridge_config = web_config.get("task_bridge", {})
        
        bridge_host = task_bridge_config.get("host", "127.0.0.1")
        bridge_port = task_bridge_config.get("port", 18766)
        
        _bridge_client = TaskBridgeClient(
            bridge_host=bridge_host,
            bridge_port=bridge_port
        )
    
    return _bridge_client


@router.websocket("/ws/tasks")
async def websocket_tasks_endpoint(websocket: WebSocket):
    """WebSocket 任务实时推送接口
    
    浏览器连接后：
    1. 立即收到当前任务树快照
    2. 后续收到所有任务更新事件
    
    消息格式：
    {
      "type": "task_update" | "initial_snapshot",
      "timestamp": "...",
      "event": {...},
      "task_tree": {...}
    }
    """
    # 获取桥接客户端
    bridge_client = get_bridge_client()
    
    # 接受连接
    await websocket.accept()
    _rlog.info("web_api", "[TaskWebSocket] 浏览器客户端已连接")
    
    # 添加到客户端池
    await bridge_client.add_browser_connection(websocket)
    
    try:
        # 保持连接
        while True:
            data = await websocket.receive_text()
            _rlog.debug("web_api", f"[TaskWebSocket] 收到浏览器消息: {data[:100]}")
    
    except WebSocketDisconnect:
        _rlog.info("web_api", "[TaskWebSocket] 浏览器客户端断开连接")
    except Exception as e:
        _rlog.error("web_api", f"[TaskWebSocket] 连接错误: {e}")
    finally:
        await bridge_client.remove_browser_connection(websocket)
        _rlog.info("web_api", "[TaskWebSocket] 连接已清理")
