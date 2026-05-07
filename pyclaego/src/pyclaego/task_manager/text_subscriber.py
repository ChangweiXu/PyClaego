"""TextSubscriber - 文本文件订阅者（异步版本）

将任务树实时更新到文本文件中，使用多层目录结构展示任务层级。

功能：
- 每次收到任务更新时，推送完整的任务树
- 使用异步队列保证顺序推送（无轮询）
- 推送带上时间戳（精确到毫秒）
- 订阅者通过队列收到后更新时间戳，重写文件
- 使用目录结构展示：session → task → subtask

**v2.0 更新**: 从同步线程改为纯异步，消除 CPU 轮询。
"""

import asyncio
import json
from asyncio import Queue as AsyncQueue
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .base import BaseSubscriber, EventType, TaskStatus
from .event import TaskEvent


class TextSubscriber(BaseSubscriber):
    """文本文件订阅者 - 将任务树实时写入文本文件（异步版本）
    
    特点：
    - 使用 asyncio.Queue 确保顺序推送更新（无轮询）
    - 每次更新时重写整个任务树文件
    - 使用多层目录结构组织文件（session/task_id/subtask_id）
    - 时间戳精确到毫秒
    - **v2.0**: 从同步线程改为异步 Task，消除 CPU 轮询
    
    文件结构：
    output_dir/
      ├── task_tree.txt        # 全局任务树视图
      ├── sessions/
      │   ├── session_a/
      │   │   ├── overview.txt  # Session 概览
      │   │   └── tasks/
      │   │       ├── task_1.txt
      │   │       └── task_2/
      │   │           ├── info.txt
      │   │           └── subtasks/
      │   │               └── subtask_1.txt
      │   └── session_b/
      │       └── ...
      └── updates.log           # 更新日志
    """
    
    def __init__(
        self,
        output_dir: Path,
        subscriber_id: str = "text_subscriber",
        auto_start: bool = True,
    ):
        """初始化文本订阅者
        
        Args:
            output_dir: 输出目录路径
            subscriber_id: 订阅者ID
            auto_start: 是否自动启动后台更新线程
        """
        super().__init__(subscriber_id)
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建子目录
        self.sessions_dir = self.output_dir / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)
        
        # 异步队列和任务
        self._update_queue: AsyncQueue = AsyncQueue()
        self._running = False
        self._worker_task: asyncio.Task | None = None
        
        # 缓存当前任务树状态（用于重建完整视图）
        self._task_cache: dict[str, dict[str, Any]] = {}  # {session_id: {task_id: task_dict}}
        self._lock = Lock()  # 保护 _task_cache（虽然是异步，但 _process_update 是同步的）
        
        # 订阅所有事件（空集合表示订阅所有）
        self._subscribed_events = set()
        
        if auto_start:
            self.start()
    
    def start(self) -> None:
        """启动后台异步任务"""
        if self._running:
            return
        
        self._running = True
        # 创建异步任务（在当前事件循环中）
        try:
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._worker_loop())
        except RuntimeError:
            # 如果没有运行中的事件循环，延迟创建
            pass
    
    async def stop(self) -> None:
        """停止后台异步任务"""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
    
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
            except RuntimeError:
                # 如果没有运行中的事件循环，会在第一次 await 时自动创建
                pass
        
        # 放入队列，带时间戳（精确到毫秒）
        update_data = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
        }
        await self._update_queue.put(update_data)
    
    async def _worker_loop(self) -> None:
        """后台异步任务 - 从队列中取出更新并处理（无轮询）"""
        while self._running:
            try:
                # ✅ 无轮询！直接 await，队列为空时自动挂起
                update_data = await self._update_queue.get()
                
                # 更新接收时间戳
                receive_timestamp = datetime.now().isoformat(timespec="milliseconds")
                
                # 处理更新
                self._process_update(update_data, receive_timestamp)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log_error(f"处理更新失败: {e}")
    
    def _process_update(self, update_data: dict[str, Any], receive_timestamp: str) -> None:
        """处理单个更新
        
        Args:
            update_data: 更新数据（包含原始时间戳和事件）
            receive_timestamp: 接收时间戳
        """
        event: TaskEvent = update_data["event"]
        send_timestamp = update_data["timestamp"]
        
        # 更新缓存
        with self._lock:
            self._update_task_cache(event)
            
            # 重写所有文件
            self._write_global_tree(send_timestamp, receive_timestamp)
            self._write_session_files(event.session_id)
            self._write_update_log(event, send_timestamp, receive_timestamp)
    
    def _update_task_cache(self, event: TaskEvent) -> None:
        """更新任务缓存
        
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
    
    def _write_global_tree(self, send_timestamp: str, receive_timestamp: str) -> None:
        """写入全局任务树视图
        
        Args:
            send_timestamp: 发送时间戳
            receive_timestamp: 接收时间戳
        """
        output_path = self.output_dir / "task_tree.txt"
        
        lines = [
            "=" * 80,
            "TaskManager - 全局任务树",
            "=" * 80,
            f"发送时间: {send_timestamp}",
            f"接收时间: {receive_timestamp}",
            f"延迟: {self._calculate_delay(send_timestamp, receive_timestamp)} ms",
            "=" * 80,
            "",
        ]
        
        # 遍历所有 Session
        for session_id, tasks in sorted(self._task_cache.items()):
            if not tasks:
                continue
            
            lines.append(f"📂 Session: {session_id}")
            lines.append(f"   任务数: {len(tasks)}")
            
            # 找出根任务（没有 parent_id 的任务）
            root_tasks = [t for t in tasks.values() if not t.get("parent_id")]
            
            for root_task in root_tasks:
                self._append_task_tree(lines, root_task, tasks, indent=1)
            
            lines.append("")
        
        # 写入文件
        output_path.write_text("\n".join(lines), encoding="utf-8")
    
    def _append_task_tree(
        self,
        lines: list[str],
        task: dict[str, Any],
        all_tasks: dict[str, dict[str, Any]],
        indent: int = 0,
    ) -> None:
        """递归追加任务树
        
        Args:
            lines: 输出行列表
            task: 当前任务
            all_tasks: 所有任务字典
            indent: 缩进层级
        """
        task_id = task["task_id"]
        task_name = task["name"]
        task_type = task["task_type"]
        status = task["status"]
        progress = task.get("progress", 0.0)
        
        # 状态图标
        status_icon = self._get_status_icon(status)
        
        # 格式化行
        indent_str = "   " * indent
        line = f"{indent_str}├─ {status_icon} [{task_type}] {task_name}"
        
        if status == TaskStatus.RUNNING.value and progress > 0:
            line += f" ({int(progress * 100)}%)"
        
        lines.append(line)
        
        # 递归追加子任务
        children = [t for t in all_tasks.values() if t.get("parent_id") == task_id]
        for child in children:
            self._append_task_tree(lines, child, all_tasks, indent + 1)
    
    def _write_session_files(self, session_id: str) -> None:
        """写入 Session 的详细文件
        
        Args:
            session_id: Session ID
        """
        tasks = self._task_cache.get(session_id, {})
        if not tasks:
            return
        
        session_dir = self.sessions_dir / session_id
        session_dir.mkdir(exist_ok=True)
        
        # 写入概览
        overview_path = session_dir / "overview.txt"
        lines = [
            f"Session: {session_id}",
            f"任务总数: {len(tasks)}",
            f"更新时间: {datetime.now().isoformat(timespec='milliseconds')}",
            "",
            "任务列表:",
        ]
        
        for task_id, task in sorted(tasks.items()):
            status_icon = self._get_status_icon(task["status"])
            lines.append(f"  {status_icon} {task['name']} ({task['task_type']})")
        
        overview_path.write_text("\n".join(lines), encoding="utf-8")
        
        # 写入每个任务的详细文件
        tasks_dir = session_dir / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        
        for task_id, task in tasks.items():
            self._write_task_file(tasks_dir, task, tasks)
    
    def _write_task_file(
        self,
        tasks_dir: Path,
        task: dict[str, Any],
        all_tasks: dict[str, dict[str, Any]],
    ) -> None:
        """写入单个任务的详细文件
        
        Args:
            tasks_dir: 任务目录
            task: 任务数据
            all_tasks: 所有任务字典
        """
        task_id = task["task_id"]
        
        # 如果有子任务，创建目录；否则创建文件
        children = [t for t in all_tasks.values() if t.get("parent_id") == task_id]
        
        if children:
            # 有子任务，创建目录
            task_dir = tasks_dir / task_id
            task_dir.mkdir(exist_ok=True)
            
            # 写入任务信息
            info_path = task_dir / "info.txt"
            info_path.write_text(self._format_task_detail(task), encoding="utf-8")
            
            # 写入子任务
            subtasks_dir = task_dir / "subtasks"
            subtasks_dir.mkdir(exist_ok=True)
            
            for child in children:
                self._write_task_file(subtasks_dir, child, all_tasks)
        else:
            # 无子任务，写入文件
            task_file = tasks_dir / f"{task_id}.txt"
            task_file.write_text(self._format_task_detail(task), encoding="utf-8")
    
    def _format_task_detail(self, task: dict[str, Any]) -> str:
        """格式化任务详细信息
        
        Args:
            task: 任务数据
            
        Returns:
            格式化后的文本
        """
        lines = [
            f"任务 ID: {task['task_id']}",
            f"任务名称: {task['name']}",
            f"任务类型: {task['task_type']}",
            f"状态: {self._get_status_icon(task['status'])} {task['status']}",
            f"进度: {int(task.get('progress', 0) * 100)}%",
            f"创建时间: {task.get('created_at', 'N/A')}",
            f"开始时间: {task.get('started_at', 'N/A')}",
            f"结束时间: {task.get('finished_at', 'N/A')}",
            "",
        ]
        
        # 描述
        if task.get("description"):
            lines.append(f"描述: {task['description']}")
            lines.append("")
        
        # 元数据
        if task.get("metadata"):
            lines.append("元数据:")
            for key, value in task["metadata"].items():
                lines.append(f"  {key}: {value}")
            lines.append("")
        
        # 结果
        if task.get("result"):
            lines.append("结果:")
            lines.append(f"  {json.dumps(task['result'], ensure_ascii=False, indent=2)}")
            lines.append("")
        
        # 错误
        if task.get("error"):
            lines.append(f"错误: {task['error']}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _write_update_log(
        self,
        event: TaskEvent,
        send_timestamp: str,
        receive_timestamp: str,
    ) -> None:
        """写入更新日志
        
        Args:
            event: 任务事件
            send_timestamp: 发送时间戳
            receive_timestamp: 接收时间戳
        """
        log_path = self.output_dir / "updates.log"
        
        delay_ms = self._calculate_delay(send_timestamp, receive_timestamp)
        
        log_entry = (
            f"[{receive_timestamp}] "
            f"{event.event_type.name} | "
            f"Session: {event.session_id} | "
            f"Task: {event.task_id} | "
            f"延迟: {delay_ms} ms\n"
        )
        
        # 追加到日志文件
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
    
    def _log_error(self, message: str) -> None:
        """记录错误日志
        
        Args:
            message: 错误消息
        """
        error_log_path = self.output_dir / "errors.log"
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        
        with open(error_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    
    @staticmethod
    def _get_status_icon(status: str) -> str:
        """获取状态图标
        
        Args:
            status: 任务状态
            
        Returns:
            状态图标
        """
        icon_map = {
            TaskStatus.PENDING.value: "⏳",
            TaskStatus.RUNNING.value: "🔄",
            TaskStatus.COMPLETED.value: "✅",
            TaskStatus.FAILED.value: "❌",
            TaskStatus.CANCELLED.value: "🚫",
        }
        return icon_map.get(status, "❓")
    
    @staticmethod
    def _calculate_delay(send_timestamp: str, receive_timestamp: str) -> float:
        """计算延迟（毫秒）
        
        Args:
            send_timestamp: 发送时间戳
            receive_timestamp: 接收时间戳
            
        Returns:
            延迟（毫秒）
        """
        try:
            send_dt = datetime.fromisoformat(send_timestamp)
            receive_dt = datetime.fromisoformat(receive_timestamp)
            delta = receive_dt - send_dt
            return delta.total_seconds() * 1000
        except Exception:
            return 0.0
