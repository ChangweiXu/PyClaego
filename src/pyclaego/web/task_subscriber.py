"""WebTaskSubscriber - Web 任务订阅者

将任务树实时推送到 WebSocket 客户端，支持多连接并发。

功能：
- 每次收到任务更新时，推送完整的任务树
- 使用异步队列保证顺序推送
- 支持多个 WebSocket 客户端同时连接
- 自动清理断开的连接
- 连接时发送当前任务树快照

**架构说明**：
- WebTaskSubscriber 实例在 CoreScheduler 进程的 SessionManager 中创建
- 通过全局注册机制，Web Server 可以获取到这个实例
- Web Server 的 WebSocket 路由将客户端连接添加到订阅者的连接池
"""

import asyncio
import json
from datetime import datetime
from threading import Lock
from typing import Dict, Any, Optional, List, Set
from fastapi import WebSocket

from ..task_manager.base import BaseSubscriber, EventType, TaskStatus
from ..task_manager.event import TaskEvent
from ..logging import get_running_log

_rlog = get_running_log()

# 全局订阅者实例（在 SessionManager 中注册）
_global_web_subscriber: Optional['WebTaskSubscriber'] = None


def set_global_web_subscriber(subscriber: 'WebTaskSubscriber') -> None:
    """设置全局 Web 订阅者实例（由 SessionManager 调用）
    
    Args:
        subscriber: WebTaskSubscriber 实例
    """
    global _global_web_subscriber
    _global_web_subscriber = subscriber
    _rlog.info("web_api", "[WebTaskSubscriber] 全局订阅者已注册")


def get_global_web_subscriber() -> Optional['WebTaskSubscriber']:
    """获取全局 Web 订阅者实例（由 Web Server 调用）
    
    Returns:
        WebTaskSubscriber 实例，如果未注册则返回 None
    """
    return _global_web_subscriber


class WebTaskSubscriber(BaseSubscriber):
    """Web 任务订阅者 - 将任务树实时推送到 WebSocket 客户端
    
    特点：
    - 使用 asyncio.Queue 确保顺序推送更新
    - 支持多个 WebSocket 客户端同时连接
    - 每次更新时推送完整的任务树
    - 自动清理断开的连接
    - 连接时立即发送当前任务树快照
    
    推送消息格式：
    {
      "type": "task_update",
      "timestamp": "2026-04-11T11:00:00.123",
      "event": {
        "event_type": "TASK_PROGRESS",
        "session_id": "my_session",
        "task_id": "task_123",
        "task_snapshot": {...}
      },
      "task_tree": {
        "session_a": [...],
        "session_b": [...]
      }
    }
    """
    
    def __init__(self, subscriber_id: str = "web_task_subscriber"):
        """初始化 Web 任务订阅者
        
        Args:
            subscriber_id: 订阅者ID
        """
        super().__init__(subscriber_id)
        
        # WebSocket 连接池
        self._connections: Set[WebSocket] = set()
        
        # 异步队列
        self._update_queue: asyncio.Queue = asyncio.Queue()
        
        # 后台任务
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        
        # 缓存任务树（与 TextSubscriber 相同）
        self._task_cache: Dict[str, Dict[str, Any]] = {}  # {session_id: {task_id: task_dict}}
        self._lock = Lock()
        
        # 订阅所有事件
        self._subscribed_events = set()
        
        _rlog.info("web_api", "[WebTaskSubscriber] 初始化完成")
    
    def start(self) -> None:
        """启动后台异步任务"""
        if self._running:
            return
        
        self._running = True
        try:
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._worker_loop())
            _rlog.info("web_api", "[WebTaskSubscriber] 后台任务已启动")
        except RuntimeError:
            # 如果没有运行中的事件循环，延迟创建
            _rlog.warning("web_api", "[WebTaskSubscriber] 无运行中的事件循环，延迟启动")
    
    async def stop(self) -> None:
        """停止后台异步任务"""
        _rlog.info("web_api", "[WebTaskSubscriber] 正在停止...")
        self._running = False
        
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        # 断开所有连接
        connections = self._connections.copy()
        for ws in connections:
            try:
                await ws.close()
            except Exception:
                pass
        
        self._connections.clear()
        _rlog.info("web_api", "[WebTaskSubscriber] 已停止")
    
    async def add_connection(self, ws: WebSocket) -> None:
        """添加 WebSocket 连接
        
        Args:
            ws: WebSocket 连接对象
        """
        self._connections.add(ws)
        _rlog.info("web_api", f"[WebTaskSubscriber] 新连接已添加，当前连接数: {len(self._connections)}")
        
        # 启动 worker（如果尚未启动）
        if not self._running:
            self.start()
        
        # 发送当前任务树快照
        await self._send_initial_snapshot(ws)
    
    async def remove_connection(self, ws: WebSocket) -> None:
        """移除 WebSocket 连接
        
        Args:
            ws: WebSocket 连接对象
        """
        self._connections.discard(ws)
        _rlog.info("web_api", f"[WebTaskSubscriber] 连接已移除，当前连接数: {len(self._connections)}")
    
    async def on_event(self, event: TaskEvent) -> None:
        """接收任务事件，放入队列等待处理（异步）
        
        Args:
            event: 任务事件对象
        """
        # 确保 worker 任务已启动
        if self._worker_task is None or self._worker_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._running = True
                self._worker_task = loop.create_task(self._worker_loop())
                _rlog.info("web_api", "[WebTaskSubscriber] Worker 任务已重启")
            except RuntimeError:
                pass
        
        # 放入队列，带时间戳（精确到毫秒）
        update_data = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
        }
        await self._update_queue.put(update_data)
    
    async def _worker_loop(self) -> None:
        """后台异步任务 - 从队列中取出更新并广播"""
        _rlog.info("web_api", "[WebTaskSubscriber] Worker loop 开始运行")
        
        while self._running:
            try:
                # 无轮询！直接 await，队列为空时自动挂起
                update_data = await self._update_queue.get()
                
                # 处理更新
                event: TaskEvent = update_data["event"]
                
                # 更新缓存
                with self._lock:
                    self._update_task_cache(event)
                    task_tree = self._build_task_tree()
                
                # 构造推送消息
                message = {
                    "type": "task_update",
                    "timestamp": update_data["timestamp"],
                    "event": event.to_dict(),
                    "task_tree": task_tree,
                }
                
                # 广播到所有连接
                await self._broadcast(message)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                _rlog.error("web_api", f"[WebTaskSubscriber] Worker loop 错误: {e}")
    
    async def _send_initial_snapshot(self, ws: WebSocket) -> None:
        """发送当前任务树快照到新连接
        
        Args:
            ws: WebSocket 连接对象
        """
        with self._lock:
            task_tree = self._build_task_tree()
        
        message = {
            "type": "initial_snapshot",
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "task_tree": task_tree,
        }
        
        try:
            message_json = json.dumps(message, ensure_ascii=False)
            await ws.send_text(message_json)
            _rlog.info("web_api", f"[WebTaskSubscriber] 已发送初始快照，包含 {len(task_tree)} 个 Session")
        except Exception as e:
            _rlog.error("web_api", f"[WebTaskSubscriber] 发送初始快照失败: {e}")
    
    async def _broadcast(self, message: Dict[str, Any]) -> None:
        """广播消息到所有连接，自动清理断开的连接
        
        Args:
            message: 要广播的消息字典
        """
        if not self._connections:
            return
        
        message_json = json.dumps(message, ensure_ascii=False)
        
        # 复制集合避免迭代时修改
        connections = self._connections.copy()
        
        disconnected = []
        for ws in connections:
            try:
                await ws.send_text(message_json)
            except Exception as e:
                _rlog.warning("web_api", f"[WebTaskSubscriber] 连接发送失败，将清理: {e}")
                disconnected.append(ws)
        
        # 清理断开的连接
        for ws in disconnected:
            self._connections.discard(ws)
        
        if disconnected:
            _rlog.info("web_api", f"[WebTaskSubscriber] 已清理 {len(disconnected)} 个断开的连接")
    
    def _update_task_cache(self, event: TaskEvent) -> None:
        """更新任务缓存（与 TextSubscriber 相同逻辑）
        
        Args:
            event: 任务事件
        """
        session_id = event.session_id
        task_id = event.task_id
        task_snapshot = event.task_snapshot
        
        # 确保 session 存在
        if session_id not in self._task_cache:
            self._task_cache[session_id] = {}
        
        # 更新或删除任务
        if event.event_type == EventType.TASK_CANCELLED:
            # 任务取消，从缓存中移除
            self._task_cache[session_id].pop(task_id, None)
        else:
            # 更新任务快照
            self._task_cache[session_id][task_id] = task_snapshot
    
    def _build_task_tree(self) -> Dict[str, List[Dict[str, Any]]]:
        """构建完整任务树结构
        
        Returns:
            {
                "session_a": [
                    {
                        "task_id": "task_1",
                        "name": "User Message",
                        "status": "running",
                        "children": [...]
                    }
                ]
            }
        """
        result = {}
        
        for session_id, tasks in self._task_cache.items():
            if not tasks:
                continue
            
            # 找出根任务（没有 parent_id 的任务）
            root_tasks = [t for t in tasks.values() if not t.get("parent_id")]
            
            session_tree = []
            for root_task in root_tasks:
                session_tree.append(self._build_task_node(root_task, tasks))
            
            result[session_id] = session_tree
        
        return result
    
    def _build_task_node(
        self,
        task: Dict[str, Any],
        all_tasks: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """递归构建任务节点（包含子任务）
        
        Args:
            task: 当前任务
            all_tasks: 所有任务字典
            
        Returns:
            任务节点字典（包含子任务）
        """
        node = {
            "task_id": task["task_id"],
            "name": task["name"],
            "task_type": task["task_type"],
            "status": task["status"],
            "progress": task.get("progress", 0.0),
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "metadata": task.get("metadata", {}),
            "description": task.get("description", ""),
            "result": task.get("result"),
            "error": task.get("error"),
            "children": []
        }
        
        # 递归处理子任务
        children = [t for t in all_tasks.values() if t.get("parent_id") == task["task_id"]]
        for child in children:
            node["children"].append(self._build_task_node(child, all_tasks))
        
        return node
