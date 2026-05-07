"""WebSocket Bridge - 任务事件桥接服务

在 CoreScheduler 进程中启动一个内部 WebSocket 服务器，
用于向 Web Server 进程推送任务事件。

架构：
    CoreScheduler 进程                     Web Server 进程
    ┌─────────────────────┐                ┌──────────────────┐
    │ TaskManager         │                │                  │
    │   │                 │                │                  │
    │   v                 │                │                  │
    │ BridgeSubscriber    │                │                  │
    │   │                 │    内部WebSocket   │  BridgeClient    │
    │   └──> 推送事件     │ ══════════════> │  (接收事件)      │
    │                     │ ws://127.0.0.1 │   │              │
    │ (18766端口)         │      :18766     │   v              │
    └─────────────────────┘                │ 广播到浏览器      │
                                          └──────────────────┘
"""

import asyncio
import json
import traceback
from collections import deque
from datetime import datetime
from typing import Any

import websockets

from ..logging import get_running_log
from ..task_manager.base import BaseSubscriber, EventType
from ..task_manager.event import TaskEvent

_rlog = get_running_log()

# 单条 snapshot_logs_chunk 内最多包含的日志条数
LOGS_PER_CHUNK = 100


class TaskBridgeServer(BaseSubscriber):
    """任务事件桥接服务器（运行在 CoreScheduler 进程）
    
    功能：
    - 订阅 TaskManager 的任务事件
    - 启动内部 WebSocket 服务器（端口 18766）
    - 将任务事件推送到所有连接的 Web Server 客户端
    """
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18766,
        subscriber_id: str = "task_bridge_server"
    ):
        super().__init__(subscriber_id)
        self.host = host
        self.port = port
        
        # WebSocket 客户端连接池（Web Server 进程）
        self._clients: set[websockets.WebSocketServerProtocol] = set()
        
        # 异步队列
        self._update_queue: asyncio.Queue = asyncio.Queue()
        
        # 服务器任务
        self._server = None
        self._worker_task: asyncio.Task | None = None
        self._running = False
        
        # 任务缓存（用于发送初始快照）
        self._task_cache: dict[str, dict[str, Any]] = {}
        
        # 任务日志缓存（task_id -> deque of log entries, capped at 200）
        self._task_logs: dict[str, deque] = {}
        self._max_logs_per_task = 200
        
        _rlog.info("core_service", f"[TaskBridge] 初始化桥接服务器: {host}:{port}")
    
    async def start(self) -> None:
        """启动桥接服务器"""
        if self._running:
            return
        
        self._running = True
        
        # 启动 worker 任务
        self._worker_task = asyncio.create_task(self._worker_loop())
        
        # 启动 WebSocket 服务器
        try:
            self._server = await websockets.serve(
                self._handle_client,
                self.host,
                self.port,
                max_size=2**23,  # 8MB - 分块发送后单条消息不再膨胀
            )
            _rlog.info("core_service", f"[TaskBridge] 桥接服务器已启动: ws://{self.host}:{self.port}")
        except Exception as e:
            _rlog.error("core_service", f"[TaskBridge] 启动失败: {e}")
            self._running = False
            raise
    
    async def stop(self) -> None:
        """停止桥接服务器"""
        _rlog.info("core_service", "[TaskBridge] 正在停止...")
        self._running = False
        
        # 停止 worker
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        # 关闭所有客户端连接
        clients = self._clients.copy()
        for client in clients:
            try:
                await client.close()
            except Exception:
                pass
        
        self._clients.clear()
        
        # 停止服务器
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        
        _rlog.info("core_service", "[TaskBridge] 已停止")
    
    async def on_event(self, event: TaskEvent) -> None:
        """接收任务事件，放入队列"""
        if event.event_type == EventType.TASK_LOG:
            # 日志事件：追加到日志缓存，不更新任务缓存
            self._append_task_log(event)
            update_data = {
                "type": "task_log",
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "task_id": event.task_id,
                "session_id": event.session_id,
                "log_level": event.extra.get("log_level", "info"),
                "log_message": event.extra.get("log_message", ""),
            }
        else:
            # 状态事件：更新缓存，仅发送增量更新（不再发送完整 task_cache）
            self._update_task_cache(event)
            update_data = {
                "type": "task_update",
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "session_id": event.session_id,
                "task_id": event.task_id,
                "task_snapshot": event.task_snapshot,
                "event": event.to_dict(),
            }
        
        await self._update_queue.put(update_data)
    
    async def _worker_loop(self) -> None:
        """后台任务 - 从队列取出更新并广播"""
        _rlog.info("core_service", "[TaskBridge] Worker loop 开始运行")
        
        while self._running:
            try:
                update_data = await self._update_queue.get()
                
                # 广播到所有连接的 Web Server 客户端
                if self._clients:
                    message_json = json.dumps(update_data, ensure_ascii=False)
                    await self._broadcast(message_json)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                _rlog.error("core_service", f"[TaskBridge] Worker loop 错误: {e}")
    
    async def _handle_client(self, websocket: websockets.WebSocketServerProtocol) -> None:
        """处理 Web Server 客户端连接
        
        连接流程：
        1. 分块发送初始快照（每个 session 一个 snapshot_chunk）
        2. 发送 snapshot_done 标记
        3. 加入广播列表，开始接收增量更新
        """
        client_id = id(websocket)
        _rlog.info("core_service", f"[TaskBridge] Web Server 客户端已连接 #{client_id}")
        
        # 对 task_cache 做一次深拷贝，避免迭代期间被 on_event 修改导致
        # RuntimeError（dict changed size）或数据不一致
        cache_snapshot = {
            sid: dict(tasks) for sid, tasks in self._task_cache.items()
        }
        logs_snapshot = {
            tid: list(logs) for tid, logs in self._task_logs.items()
        }
        
        # 快照拷贝完成后立即加入广播列表——此后发生的增量更新会通过
        # _worker_loop 推送给该客户端，确保不丢失快照拷贝之后的事件
        self._clients.add(websocket)
        
        # 分块发送初始快照
        try:
            # 第一阶段：每个 session 一条 snapshot_chunk（仅含 tasks，不含日志）
            for session_id, tasks in cache_snapshot.items():
                chunk = {
                    "type": "snapshot_chunk",
                    "session_id": session_id,
                    "tasks": tasks,
                }
                await websocket.send(json.dumps(chunk, ensure_ascii=False))

            # 第二阶段：按 task 拆分日志，每片最多 LOGS_PER_CHUNK 条
            # 反向索引 task_id -> session_id，跳过孤儿日志
            task_to_session: dict[str, str] = {
                task_id: sid
                for sid, tasks in cache_snapshot.items()
                for task_id in tasks
            }
            logs_chunks_count = 0
            for task_id, logs in logs_snapshot.items():
                if not logs:
                    continue
                session_id = task_to_session.get(task_id)
                if session_id is None:
                    continue
                total = len(logs)
                seq = 0
                for start in range(0, total, LOGS_PER_CHUNK):
                    end = start + LOGS_PER_CHUNK
                    is_last = end >= total
                    log_chunk = {
                        "type": "snapshot_logs_chunk",
                        "task_id": task_id,
                        "session_id": session_id,
                        "logs": logs[start:end],
                        "seq": seq,
                        "is_last": is_last,
                    }
                    await websocket.send(json.dumps(log_chunk, ensure_ascii=False))
                    seq += 1
                    logs_chunks_count += 1

            # 第三阶段：发送快照结束标记
            done = {
                "type": "snapshot_done",
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            }
            await websocket.send(json.dumps(done, ensure_ascii=False))
            _rlog.info(
                "core_service",
                f"[TaskBridge] 已发送分块快照到客户端 #{client_id}，"
                f"sessions={len(cache_snapshot)}, log_chunks={logs_chunks_count}",
            )
        except Exception as e:
            _rlog.error("core_service", f"[TaskBridge] 发送初始快照失败: {e}\n{traceback.format_exc()}")
            self._clients.discard(websocket)
            return
        _rlog.info("core_service", f"[TaskBridge] 客户端 #{client_id} 已加入广播，当前连接数: {len(self._clients)}")
        
        try:
            # 保持连接
            async for message in websocket:
                # Web Server 可以发送心跳或控制命令
                _rlog.debug("core_service", f"[TaskBridge] 收到客户端消息: {message[:100]}")
        except websockets.exceptions.ConnectionClosed:
            _rlog.info("core_service", f"[TaskBridge] 客户端 #{client_id} 断开连接")
        except Exception as e:
            _rlog.error("core_service", f"[TaskBridge] 客户端 #{client_id} 错误: {e}\n{traceback.format_exc()}")
        finally:
            self._clients.discard(websocket)
            _rlog.info("core_service", f"[TaskBridge] 客户端 #{client_id} 已清理，当前连接数: {len(self._clients)}")
    
    async def _broadcast(self, message: str) -> None:
        """广播消息到所有 Web Server 客户端"""
        clients = self._clients.copy()
        disconnected = []
        
        for client in clients:
            try:
                await client.send(message)
            except Exception as e:
                _rlog.warning("core_service", f"[TaskBridge] 广播失败，将清理客户端: {e}")
                disconnected.append(client)
        
        # 清理断开的客户端
        for client in disconnected:
            self._clients.discard(client)
    
    def _update_task_cache(self, event: TaskEvent) -> None:
        """更新任务缓存"""
        session_id = event.session_id
        task_id = event.task_id
        task_snapshot = event.task_snapshot
        
        # 确保 session 存在
        if session_id not in self._task_cache:
            self._task_cache[session_id] = {}
        
        # 更新任务快照
        self._task_cache[session_id][task_id] = task_snapshot

    def _append_task_log(self, event: TaskEvent) -> None:
        """追加任务日志条目"""
        task_id = event.task_id
        if task_id not in self._task_logs:
            self._task_logs[task_id] = deque(maxlen=self._max_logs_per_task)
        
        self._task_logs[task_id].append({
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "level": event.extra.get("log_level", "info"),
            "message": event.extra.get("log_message", ""),
        })
