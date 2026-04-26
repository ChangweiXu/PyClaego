"""Session 类 - 表示一个用户会话"""

import os
import json
import asyncio
import traceback
from typing import Dict, Any, Optional, List, Callable, Awaitable
from datetime import datetime
from pathlib import Path
import uuid

import yaml

from ..config import get_config, get_session_config
from ..utility import validate_session_id
from ..agent import AgentFactory, BaseAgent
from ..context import ContextFactory, BaseContextHandler, BaseContextHandlerV3
from ..context.system_prompts.default_soul import DEFAULT_SOUL
from ..task_manager import SessionTaskHandlerV2, TaskManager, TaskType
from ..logging import get_running_log
from .cron import SessionCronScheduler, CronJob, slugify

_rlog = get_running_log()


class Session:
    """用户会话类
    
    功能：
    - 管理独立的工作空间
    - 存储会话历史和状态
    - 跟踪订阅状态
    """
    
    def __init__(
        self,
        session_id: str,
        user_id: str = "default_user",
        workspace_root: str = "./workspaces",
    ):
        """初始化 Session
        
        Args:
            session_id: 会话ID (只能包含小写字母、数字和下划线)
            user_id: 用户ID
            workspace_root: 工作空间根目录
            
        Raises:
            ValueError: session_id 格式不合法
        """
        # 验证 session_id 格式
        if not validate_session_id(session_id):
            raise ValueError(
                f"Invalid session_id format: '{session_id}'. "
                "Session ID must contain only lowercase letters, digits, and underscores."
            )
        
        self.session_id = session_id
        self.user_id = user_id
        self.workspace_root = Path(workspace_root)
        
        # 读取全局配置,获取 session_workspace_root 映射
        config = get_config()
        session_config = config.get("session", {})
        session_workspace_root_dict = session_config.get("session_workspace_root", {})
        
        # 【2026年03月31日16:35:03新增】
        # 如果 session_id 在 session_workspace_root 字典中,使用配置的路径
        if session_workspace_root_dict and session_id in session_workspace_root_dict:
            custom_workspace = session_workspace_root_dict[session_id]
            self.workspace_path = Path(custom_workspace)
            _rlog.info(f"session_{session_id}", f"[Session] 使用自定义工作目录: {self.workspace_path}")
        else:
            # 否则使用默认路径
            self.workspace_path = self.workspace_root / session_id
            _rlog.info(f"session_{session_id}", f"[Session] 使用默认工作目录: {self.workspace_path}")
        
        # 状态信息
        self.created_at = datetime.now()
        self.last_active = datetime.now()
        self.is_subscribed = False  # 是否有客户端订阅
        self.subscriber_count = 0   # 订阅者数量

        # 确保工作空间存在（不再加载历史记录，历史由 context handler 管理）
        self._ensure_workspace()
        
        # 【2026年03月31日16:35:26新增】
        # 获取 Session 级配置 (自动合并全局配置和 Session 配置)
        # 传入 workspace_path 避免重复推断
        config = get_session_config(
            session_id=self.session_id,
            workspace_path=self.workspace_path
        )
        
        # 【2026年03月29日10:18:30新增】消息队列和处理锁
        self._message_queue = asyncio.Queue()
        self._processing_lock = asyncio.Lock()
        self._is_processing = False
        self._processor_task: Optional[asyncio.Task] = None
        # 【2026年03月30日20:23:00新增】当前 Agent 任务引用（用于取消）
        self._current_agent_task: Optional[asyncio.Task] = None
        
        # 初始化 Context Handler
        context_config = config.get("context", {})
        try:
            _context_handler: BaseContextHandler = ContextFactory.create_handler(
                session_id=self.session_id,
                workspace_path=self.workspace_path,
                context_config=context_config
            )
            if not isinstance(_context_handler, BaseContextHandlerV3):
                raise TypeError(f"Context handler must be a subclass of BaseContextHandlerV3, got {type(_context_handler)}")
            self.context_handler: BaseContextHandlerV3 = _context_handler
            _rlog.info(f"session_{self.session_id}", f"[Session] Context Handler 已初始化: {context_config.get('type')}")
        except Exception as e:
            _rlog.error(f"session_{self.session_id}", f"[Session] Context Handler 初始化失败: {e}\n{traceback.format_exc()}")
            # 使用默认配置重试
            raise e
        
        # 初始化 Agent
        agent_config = config.get("agent", {})
        self.agent: Optional[BaseAgent] = None
        if agent_config:
            try:
                self.agent = AgentFactory.create_agent(agent_config, self.session_id)
                _rlog.info(f"session_{self.session_id}", f"[Session] Agent 已初始化: {agent_config.get('type')}")
            except Exception as e:
                _rlog.error(f"session_{self.session_id}", f"[Session] Agent 初始化失败: {e}\n{traceback.format_exc()}")
                self.agent = None
        else:
            _rlog.info(f"session_{self.session_id}", "[Session] 未配置 Agent，将使用测试模式")
        
        # 【2026年03月29日10:18:30新增】启动消息处理器
        self._start_message_processor()
        
        # 【2026年03月30日20:23:00新增】初始化命令处理器
        from .command_handler import CommandHandler
        self.command_handler = CommandHandler(self)
        _rlog.info(f"session_{self.session_id}", "[Session] 命令处理器已初始化")

        # ──────────────────────────────────────────────────────────────────
        # 【2026年04月22日新增】Broadcast handler（由 SessionManager 注入）
        # 用于将无客户端 await 的响应（cron 等）推送给已订阅客户端。
        self._broadcast_handler: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

        # 【2026年04月22日新增】Session 级 cron 调度器（仅当 <ws>/config.yaml
        # 中存在 cron.enabled=true 时创建）
        self._shutting_down: bool = False
        cron_config = config.get("cron", {})
        self.cron: Optional[SessionCronScheduler] = self._init_cron_scheduler(cron_config)
    
    # ──────────────────────────────────────────────────────────────────
    # 【2026年04月22日新增】Cron 相关
    # ──────────────────────────────────────────────────────────────────

    def _init_cron_scheduler(self, cron_config: Dict[str, Any]) -> Optional[SessionCronScheduler]:
        """按需创建并启动 SessionCronScheduler"""
        cron_cfg = cron_config
        if not cron_cfg or not cron_cfg.get("enabled"):
            return None
        try:
            scheduler = SessionCronScheduler(self, cron_cfg)
            scheduler.start()
            _rlog.info(
                f"session_{self.session_id}",
                f"[Session] Cron 调度器已启用，任务数={len(scheduler.jobs)}",
            )
            return scheduler
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[Session] Cron 调度器初始化失败: {e}\n{traceback.format_exc()}",
            )
            return None

    def set_broadcast_handler(
        self,
        handler: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    ) -> None:
        """注入 unsolicited 广播回调（由 SessionManager 在创建 Session 时设置）

        Args:
            handler: 异步函数，签名 ``async fn(message_dict) -> None``。
                    cron 任务完成后会调用它把结果推给所有已订阅客户端。
        """
        self._broadcast_handler = handler

    async def _enqueue_cron(self, job: CronJob) -> None:
        """将 cron 任务作为合成 user_message 投入队列，处理完落盘+广播

        - 创建独立的 TaskManager 顶层任务（TaskType.USER_MESSAGE，带 cron 标记）
        - 复用 _message_queue（与普通 user message 同路径）
        - 等待响应后写入 cron 输出文件，并通过 broadcast handler 推送（如有）
        """
        fired_at = datetime.now()
        cron_user_id = f"cron:{job.name}"

        _rlog.info(
            f"session_{self.session_id}",
            f"[Session] Cron 触发: {job.name} (schedule='{job.schedule}')",
        )

        # 1. 创建任务管理器顶层任务（与 SessionManager.route_message 中等价）
        task_manager = TaskManager.get_instance()
        task_id = await task_manager.create_task(
            session_id=self.session_id,
            task_type=TaskType.USER_MESSAGE,
            name=f"Cron: {job.name}",
            parent_id=None,
            description=f"Cron-triggered prompt from job '{job.name}'",
            user_id=cron_user_id,
            source="cron",
            cron_name=job.name,
            schedule=job.schedule,
        )

        wrapped_handler = SessionTaskHandlerV2(
            session_id=self.session_id,
            user_id=cron_user_id,
            task_id=task_id,
            original_handler=None,
        )

        # 2. 合成 user_message（带 source=cron 标记，便于上下文/UI 区分）
        user_msg: Dict[str, Any] = {
            "role": "user",
            "type": "user",
            "content": job.prompt,
            "user_id": cron_user_id,
            "source": "cron",
            "cron_name": job.name,
            "timestamp": fired_at.isoformat(),
        }

        response_future: asyncio.Future = asyncio.Future()
        await self._message_queue.put({
            "type": "message",
            "message": user_msg,
            "user_id": cron_user_id,
            "response_future": response_future,
            "msg_update_handler": wrapped_handler,
        })

        # 3. 等待执行并落盘 + 广播。整段 try 包裹，保证异常不冒泡到调度器。
        try:
            response = await response_future
            await task_manager.complete_task(task_id, result=response)
        except Exception as e:
            await task_manager.fail_task(task_id, str(e))
            response = {
                "type": "response",
                "session_id": self.session_id,
                "content": f"[cron error] {e}",
                "timestamp": datetime.now().isoformat(),
                "error": str(e),
            }

        # 4. 写入文件
        if job.save_to_file:
            try:
                self._write_cron_output(job, fired_at, response)
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[Session] cron 输出落盘失败 ({job.name}): {e}\n{traceback.format_exc()}",
                )

        # 5. 广播给已订阅客户端（无订阅者也无所谓，文件已存）
        if job.broadcast and self._broadcast_handler is not None:
            try:
                broadcast_msg = {
                    "type": "cron_response",
                    "session_id": self.session_id,
                    "cron_name": job.name,
                    "schedule": job.schedule,
                    "fired_at": fired_at.isoformat(),
                    "content": response.get("content", ""),
                    "timestamp": datetime.now().isoformat(),
                }
                await self._broadcast_handler(broadcast_msg)
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[Session] cron 广播失败 ({job.name}): {e}",
                )

    def _write_cron_output(
        self,
        job: CronJob,
        fired_at: datetime,
        response: Dict[str, Any],
    ) -> Path:
        """原子写入 ``<workspace>/cron/YYYYMMDD-HHMMSS-<slug>.md``"""
        cron_dir = self.workspace_path / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)

        slug = slugify(job.name)
        filename = f"{fired_at.strftime('%Y%m%d-%H%M%S')}-{slug}.md"
        target = cron_dir / filename
        tmp = target.with_suffix(target.suffix + ".tmp")

        # YAML front-matter
        front: Dict[str, Any] = {
            "cron_name": job.name,
            "schedule": job.schedule,
            "fired_at": fired_at.isoformat(),
            "prompt": job.prompt,
        }
        if response.get("error"):
            front["error"] = response["error"]

        front_yaml = yaml.safe_dump(
            front, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        body = response.get("content", "") or ""

        text = f"---\n{front_yaml}---\n\n{body}\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        _rlog.info(
            f"session_{self.session_id}",
            f"[Session] cron 输出已写入: {target}",
        )
        return target

    async def shutdown(self) -> None:
        """优雅关闭 Session：停止 cron → 取消当前 agent task → 排空消息处理器"""
        if self._shutting_down:
            return
        self._shutting_down = True
        _rlog.info(f"session_{self.session_id}", "[Session] 开始关闭...")

        # 1. 停止 cron 调度器（阻止新触发）
        if self.cron is not None:
            try:
                self.cron.shutdown(wait=False)
            except Exception as e:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[Session] cron shutdown 异常: {e}",
                )

        # 2. 取消正在运行的 agent task
        if self._current_agent_task and not self._current_agent_task.done():
            self._current_agent_task.cancel()

        # 3. 投递哨兵让 processor loop 退出，并等待结束
        try:
            await self._message_queue.put(None)
        except Exception:
            pass
        if self._processor_task and not self._processor_task.done():
            try:
                await asyncio.wait_for(self._processor_task, timeout=10)
            except asyncio.TimeoutError:
                _rlog.warning(
                    f"session_{self.session_id}",
                    "[Session] processor 退出超时，强制取消",
                )
                self._processor_task.cancel()
            except Exception as e:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[Session] processor 退出异常: {e}",
                )

        # 4. 持久化元数据
        try:
            self._save_metadata()
        except Exception as e:
            _rlog.warning(
                f"session_{self.session_id}",
                f"[Session] 关闭时保存元数据失败: {e}",
            )

        _rlog.info(f"session_{self.session_id}", "[Session] 关闭完成")

    # ──────────────────────────────────────────────────────────────────

    def _ensure_workspace(self) -> None:
        """确保工作空间目录存在"""
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        
        # 创建会话元数据文件
        meta_file = self.workspace_path / "session.json"
        if not meta_file.exists():
            self._save_metadata()
        
        # 创建 skills/ 目录（Session 独有技能存放位置）
        skills_dir = self.workspace_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        
        # 创建 config.yaml（空配置文件，供 Session 级别覆盖使用）
        config_file = self.workspace_path / "config.yaml"
        if not config_file.exists():
            config_file.write_text("", encoding="utf-8")
        
        # 创建 SOUL.md（写入默认内容，供上下文处理器读取）
        soul_file = self.workspace_path / "SOUL.md"
        if not soul_file.exists():
            soul_file.write_text(DEFAULT_SOUL, encoding="utf-8")
    
    def _save_metadata(self) -> None:
        """保存会话元数据"""
        meta_file = self.workspace_path / "session.json"
        metadata = {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "is_subscribed": self.is_subscribed,
            "subscriber_count": self.subscriber_count
        }
        
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    def _load_history(self) -> None:
        """[已废弃] 历史消息由 context handler（SimpleContextHandler）管理，Session 不再负责加载。

        保留此方法避免外部调用报错，内部为空操作。
        """
        pass
    
    def subscribe(self) -> None:
        """订阅 Session（客户端连接）"""
        self.is_subscribed = True
        self.subscriber_count += 1
        self.last_active = datetime.now()
        self._save_metadata()
        _rlog.info(f"session_{self.session_id}", f"[Session] 订阅者连接，当前订阅数: {self.subscriber_count}")
    
    def unsubscribe(self) -> None:
        """取消订阅 Session（客户端断开）"""
        self.subscriber_count = max(0, self.subscriber_count - 1)
        if self.subscriber_count == 0:
            self.is_subscribed = False
        self.last_active = datetime.now()
        self._save_metadata()
        _rlog.info(f"session_{self.session_id}", f"[Session] 订阅者断开，当前订阅数: {self.subscriber_count}")
    
    def _start_message_processor(self) -> None:
        """启动消息处理器（后台任务）"""
        # 【2026年03月29日10:18:30新增】
        if not self._processor_task or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._message_processor_loop())
            _rlog.info(f"session_{self.session_id}", "[Session] 消息处理器已启动")
    
    async def _message_processor_loop(self) -> None:
        """消息处理器循环（持续从队列中取消息处理）"""
        # 【2026年03月29日10:18:30新增】
        _rlog.info(f"session_{self.session_id}", "[Session] 消息处理循环开始")
        
        while True:
            try:
                # 从队列中获取消息（阻塞等待）
                # 这里不会轮询，而是在队列来活的时候唤醒
                item = await self._message_queue.get()
                
                # 检查是否是停止信号
                if item is None:
                    _rlog.info(f"session_{self.session_id}", "[Session] 收到停止信号，退出处理循环")
                    break
                
                # 从 dict 中解包字段
                item_type          = item["type"]           # "message" | "command"
                message            = item["message"]
                user_id            = item["user_id"]
                response_future    = item["response_future"]
                msg_update_handler = item["msg_update_handler"]
                
                try:
                    # 使用锁确保同一时间只处理一条消息/命令
                    async with self._processing_lock:
                        self._is_processing = True
                        _rlog.info(f"session_{self.session_id}", f"[Session] 开始处理 {item_type} (队列剩余: {self._message_queue.qsize()})")
                        
                        if item_type == "command":
                            # 队列命令（如 /compress）：同样包进 task，支持 /stop 取消
                            self._current_agent_task = asyncio.create_task(
                                self.command_handler.handle_command(message, user_id)
                            )
                        else:
                            # 普通消息：创建 agent task 并跟踪
                            self._current_agent_task = asyncio.create_task(
                                self._do_process_message(
                                    message,
                                    user_id,
                                    msg_update_handler=msg_update_handler
                                )
                            )
                        # 等待任务完成（command 和 message 统一路径）
                        response = await self._current_agent_task
                        self._current_agent_task = None
                        
                        # 设置响应结果
                        if not response_future.done():
                            response_future.set_result(response)
                        
                        self._is_processing = False
                        _rlog.info(f"session_{self.session_id}", f"[Session] {item_type} 处理完成")
                
                except asyncio.CancelledError:
                    # 任务被 /stop 命令取消（message 和 command 类型的 task 均可被取消）
                    _rlog.warning(f"session_{self.session_id}", "[Session] 任务被 /stop 命令取消")
                    if not response_future.done():
                        response_future.set_result({
                            "type": "response",
                            "session_id": self.session_id,
                            "content": "⚠️ 任务已被 /stop 命令取消",
                            "timestamp": datetime.now().isoformat(),
                            "cancelled": True
                        })
                    self._is_processing = False
                    self._current_agent_task = None
                
                except Exception as e:
                    # 处理失败，设置异常
                    _rlog.error(f"session_{self.session_id}", f"[Session] 消息处理异常: {e}")
                    if not response_future.done():
                        response_future.set_exception(e)
                    self._is_processing = False
                    self._current_agent_task = None
                
                finally:
                    # 标记任务完成
                    self._message_queue.task_done()
            
            except asyncio.CancelledError:
                _rlog.warning(f"session_{self.session_id}", "[Session] 处理循环被取消")
                break
            
            except Exception as e:
                import traceback
                _rlog.error(f"session_{self.session_id}", f"[Session] 处理循环异常: {e}\n{traceback.format_exc()}")
    
    async def process_message(
        self,
        message: Dict[str, Any],
        user_id: str,
        msg_update_handler: SessionTaskHandlerV2,  # 【2026年04月16日修改】回调参数类型固定
    ) -> Dict[str, Any]:
        """处理消息（入队列或执行命令）
        
        Args:
            message: 用户消息
            user_id: 发送消息的用户ID（可选，默认使用session的user_id）
            msg_update_handler: 进度更新回调函数
            
        Returns:
            响应消息
        """
        # 检查是否为命令
        if self.command_handler.is_command(message):
            _rlog.info(f"session_{self.session_id}", f"[Session] 检测到命令: {message.get('content')}")

            if self.command_handler.is_queued_command(message):
                # 队列命令（如 /compress）：入队，等待串行执行，保证不与正在处理的消息并发
                response_future = asyncio.Future()
                await self._message_queue.put({
                    "type": "command",
                    "message": message,
                    "user_id": user_id,
                    "response_future": response_future,
                    "msg_update_handler": None,
                })
                queue_size = self._message_queue.qsize()
                _rlog.info(f"session_{self.session_id}", f"[Session] 队列命令已入队 (队列大小: {queue_size})")
                return await response_future
            else:
                # 立即命令（/stop、/help）：直接执行，不入队列
                return await self.command_handler.handle_command(message, user_id)

        # 普通消息：创建一个 Future 来接收响应，入队串行处理
        response_future = asyncio.Future()
        await self._message_queue.put({
            "type": "message",
            "message": message,
            "user_id": user_id,
            "response_future": response_future,
            "msg_update_handler": msg_update_handler,
        })

        queue_size = self._message_queue.qsize()
        _rlog.info(f"session_{self.session_id}", f"[Session] 消息已入队 (队列大小: {queue_size})")

        # 等待处理完成并返回响应
        response = await response_future
        return response
    
    async def _do_process_message(
        self,
        message: Dict[str, Any],
        user_id: str,
        msg_update_handler: SessionTaskHandlerV2,
    ) -> Dict[str, Any]:
        """实际处理消息的逻辑（队列中的消息处理）

        Args:
            message:            用户消息 dict（含 content、type 等字段）
            user_id:            发送消息的用户 ID
            msg_update_handler: 进度更新回调函数

        Returns:
            响应消息 dict
        """
        self.last_active = datetime.now()
        await msg_update_handler.start()

        msg_user_id = user_id or self.user_id
        user_content = message.get("content", "")

        # 构造完整的 user_message dict（传给 agent，由 agent 透传给 context handler 写盘）
        user_msg: Dict[str, Any] = {
            "role": "user",
            "content": user_content,
            "user_id": msg_user_id,
            "timestamp": datetime.now().isoformat(),
            "type": "user",
        }

        # 透传 content_parts（多模态内容块，如图片）
        if "content_parts" in message:
            user_msg["content_parts"] = message["content_parts"]

        # 调用 Agent 处理消息
        if self.agent:
            try:
                self.context_handler.set_session_task_handler(msg_update_handler)
                response_content = await self.agent.process_v2(
                    user_message=user_msg,
                    context_handler=self.context_handler,
                    session_task_handler=msg_update_handler,
                )
            except Exception as e:
                _rlog.error(f"session_{self.session_id}", f"[Session] Agent 处理失败: {e}")
                response_content = f"抱歉，处理您的消息时出现错误：{str(e)}"
        else:
            # 测试模式：直接返回简单响应
            response_content = f"[测试模式] 已收到来自 {msg_user_id} 的消息：{user_content}"

        return {
            "type": "response",
            "session_id": self.session_id,
            "content": response_content,
            "timestamp": datetime.now().isoformat(),
        }
    
    def _save_history(self) -> None:
        """[已废弃] 历史消息由 context handler（SimpleContextHandler）通过 HistoryFileManager 写盘。

        保留此方法避免外部调用报错，内部为空操作。
        """
        pass
    
    def get_info(self) -> Dict[str, Any]:
        """获取 Session 信息
        
        Returns:
            Session 信息字典
        """
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "workspace_path": str(self.workspace_path),
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "is_subscribed": self.is_subscribed,
            "subscriber_count": self.subscriber_count,
        }


def generate_session_id() -> str:
    """生成新的 Session ID
    
    Returns:
        格式化的 Session ID (例如: sess_abc123xyz)
    """
    return f"sess_{uuid.uuid4().hex[:12]}"
