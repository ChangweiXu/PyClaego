"""``Widget`` 运行时（Phase 1.2 / 3.5）。

每个 widget 对应 PS 内的一块持久化"小应用"：
- 拥有独立 ``workspace_dir``（``personal_spaces/<ps>/widgets/<wid>/``）
- 拥有独立 ``Agent`` + ``ContextHandler`` 实例
- 通过 ``_processing_lock`` 串行化对消息的处理（同 PS 内不同 widget
  并行；同一 widget 内消息串行）
- ``process_message`` 是唯一入口；调用方（CoreScheduler / cron）需自带
  ``WidgetTaskHandler``。Widget 不直接持有 ``TaskManager`` 单例，便于测试。

依赖注入：
- ``agent_factory`` / ``context_factory`` 可在构造时覆盖，方便单元测试
  替换为 mock。生产环境默认使用 ``AgentFactory`` / ``ContextFactory``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..logging import get_running_log
from ..task_manager import TaskBelonging
from .models import WidgetManifest

_rlog = get_running_log()


# ---- DI 类型签名 ----------------------------------------------------------
# Both factories receive the FULL widget resolved_config (whole dict);
# each factory internally extracts its own subkey (`agent` / `context`).
AgentFactoryFn = Callable[[dict[str, Any], str], Any]
ContextFactoryFn = Callable[[str, Path, dict[str, Any]], Any]


def _default_agent_factory(widget_config: dict[str, Any], session_id: str) -> Any:
    from ..agent import AgentFactory
    return AgentFactory.create_agent(widget_config, session_id)


def _default_context_factory(
    session_id: str, workspace_path: Path, widget_config: dict[str, Any]
) -> Any:
    from ..context import ContextFactory
    return ContextFactory.create_handler(
        session_id=session_id,
        workspace_path=workspace_path,
        widget_config=widget_config,
    )


class Widget:
    """单个 widget 的运行时。"""

    SOURCE_CHAT = "chat"
    SOURCE_CRON = "cron"

    def __init__(
        self,
        ps_id: str,
        manifest: WidgetManifest,
        workspace_dir: Path,
        resolved_config: dict[str, Any] | None = None,
        *,
        agent_factory: AgentFactoryFn | None = None,
        context_factory: ContextFactoryFn | None = None,
        widget_class_spec: Any | None = None,
    ) -> None:
        self.ps_id: str = ps_id
        self.manifest: WidgetManifest = manifest
        self.workspace_dir: Path = Path(workspace_dir).resolve()
        self.resolved_config: dict[str, Any] = resolved_config or {}
        # 可选：WidgetClassSpec，主要用于取 hook_class 与 default_*_file
        self.widget_class_spec: Any | None = widget_class_spec

        self.belongs_to: TaskBelonging = TaskBelonging(
            ps_id=ps_id, widget_id=manifest.widget_id
        )

        self._agent_factory: AgentFactoryFn = agent_factory or _default_agent_factory
        self._context_factory: ContextFactoryFn = (
            context_factory or _default_context_factory
        )

        # 运行时状态（load 后填充）
        self.agent: Any | None = None
        self.context_handler: Any | None = None
        self.store: Any | None = None  # WidgetStore（按 widget_config["store"] 决定是否创建）
        self.widget_tools: list = []     # 由 widget_tools.builder 构造，绑定本 widget 的 store
        self.hook: Any | None = None  # WidgetHook 实例（Phase 11，有则加载、无则 None）
        self._loaded: bool = False
        self._processing_lock: asyncio.Lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._current_request_id: str | None = None
        self._shutting_down: bool = False

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def widget_id(self) -> str:
        return self.manifest.widget_id

    @property
    def widget_class(self) -> str:
        return self.manifest.widget_class

    @property
    def session_id(self) -> str:
        """供 Agent / Context 内部日志使用的标识；与 belongs_to.key() 一致。"""
        return self.belongs_to.key()

    @property
    def current_request_id(self) -> str | None:
        """当前正在处理的请求 ID（仅 process_message 期间有效）。"""
        return self._current_request_id

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _cleanup_subagents(self, ttl_hours: float = 48.0) -> None:
        """清理 subagents 目录下超过 ttl_hours 小时的文件；目录为空则递归删除。"""
        subagents_dir = self.workspace_dir / "subagents"
        if not subagents_dir.is_dir():
            return

        cutoff = datetime.now() - timedelta(hours=ttl_hours)
        removed_files = 0
        removed_dirs = 0
        errors = 0

        # 递归删除超期文件
        for path in subagents_dir.rglob("*"):
            if path.is_file():
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime)
                    if mtime < cutoff:
                        path.unlink()
                        removed_files += 1
                except Exception:
                    errors += 1

        # 递归删除空目录（从深到浅，按路径层数倒序）
        for dirpath in sorted(
            subagents_dir.rglob("*"),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            if dirpath.is_dir():
                try:
                    if not any(dirpath.iterdir()):
                        dirpath.rmdir()
                        removed_dirs += 1
                except Exception:
                    errors += 1

        if removed_files or removed_dirs or errors:
            _rlog.info(
                "core_service",
                f"[Widget:{self.widget_id}] subagents cleanup done: "
                f"removed_files={removed_files}, removed_dirs={removed_dirs}, "
                f"errors={errors}, ttl_hours={ttl_hours}",
            )

    async def load(self) -> None:
        """构造 Agent + ContextHandler，并保证 workspace 目录存在。

        幂等：重复调用直接返回。失败时清理已构造的组件。
        """
        if self._loaded:
            return

        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # 清理过期 subagent 工作目录（48 小时 TTL）
        try:
            self._cleanup_subagents()
        except Exception:
            _rlog.exception(
                "core_service",
                f"[Widget:{self.widget_id}] subagents cleanup 异常，忽略继续加载",
            )

        # 完整 widget 配置（含 agent / context / ps_metadata / context_subagents）
        full_cfg = self.resolved_config or {}
        agent_cfg = full_cfg.get("agent") or {}
        context_cfg = full_cfg.get("context") or {}

        if not context_cfg:
            raise ValueError(
                f"Widget {self.widget_id} 缺少 'context' 配置 — "
                f"请确认 widget_class.defaults 或全局 config.yaml 中已声明。"
            )

        # Context Handler 必须先于 Agent
        try:
            self.context_handler = self._context_factory(
                self.session_id, self.workspace_dir, full_cfg
            )
        except Exception:
            self.context_handler = None
            _rlog.exception(
                "core_service",
                f"[Widget:{self.widget_id}] ContextHandler 初始化失败 (config={context_cfg})",
            )
            raise

        # WidgetStore（Phase 3.3）：按配置可选创建；失败时降级为 None，不阻塞 load
        try:
            from .datastores import create_widget_store  # 局部导入避免循环
            self.store = create_widget_store(full_cfg, self.workspace_dir)
            if self.store is not None:
                await self.store.open()
        except Exception:
            _rlog.exception(
                "core_service",
                f"[Widget:{self.widget_id}] WidgetStore 初始化失败，降级为无 store",
            )
            self.store = None

        # Widget-aware tools（Phase 3.4）：绑定本 widget 的 store / id / ps_id
        try:
            from .widget_tools import build_widget_tools  # 局部导入
            self.widget_tools = build_widget_tools(
                widget_config=full_cfg,
                store=self.store,
                ps_id=self.ps_id,
                widget_id=self.widget_id,
            )
        except Exception:
            _rlog.exception(
                "core_service",
                f"[Widget:{self.widget_id}] widget_tools 构造失败，降级为空列表",
            )
            self.widget_tools = []

        # ToolAgent 快照 — 解析本 Widget 可见的子代理配置
        self._tool_agents: dict = {}
        try:
            from ..tool_agent import get_tool_agent_manager
            tam = get_tool_agent_manager()
            self._tool_agents = tam.resolve_for_widget(self.ps_id, self.widget_id)
            _rlog.info(
                "core_service",
                f"[Widget:{self.widget_id}] tool_agents 快照: {list(self._tool_agents.keys())}",
            )
        except Exception:
            _rlog.exception(
                "core_service",
                f"[Widget:{self.widget_id}] tool_agents 解析失败，降级为空",
            )

        if agent_cfg:
            try:
                self.agent = self._agent_factory(full_cfg, self.session_id)
                # 若 agent 支持 widget_workspace 注入（SpawnAgent），告知其工作目录
                # TODO: 未来可考虑引入更通用的生命周期接口（例如 AgentContextAware）来解耦
                #       注入逻辑，而非硬编码方法名；但目前仅 SpawnAgent 需要，暂不泛化。
                inject_ws = getattr(self.agent, "inject_widget_workspace", None)
                if callable(inject_ws):
                    inject_ws(self.workspace_dir)
                # 注入本 widget 的工具集合（Phase 3.4）；agent 可决定是否暴露给 LLM
                inject_tools = getattr(self.agent, "inject_widget_tools", None)
                if callable(inject_tools) and self.widget_tools:
                    inject_tools(self.widget_tools)
                # 注入本 widget 的 tool_agents 快照
                inject_ta = getattr(self.agent, "inject_tool_agents", None)
                if callable(inject_ta) and self._tool_agents:
                    inject_ta(self._tool_agents)
            except Exception:
                self.agent = None
                _rlog.exception(
                    "core_service",
                    f"[Widget:{self.widget_id}] Agent 初始化失败 (config={agent_cfg})",
                )
                # 不抛 — 与原 Session 保持一致：无 agent 时进入 "测试模式"
        else:
            self.agent = None
            _rlog.info("core_service", f"[Widget:{self.widget_id}] 未配置 agent，将以测试模式回声")

        self._loaded = True
        # 实例化可选 WidgetHook（Phase 11）
        spec = self.widget_class_spec
        hook_cls = getattr(spec, "hook_class", None) if spec is not None else None
        if hook_cls is not None:
            try:
                self.hook = hook_cls(self)
                res = self.hook.on_create()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                _rlog.exception(
                    "core_service",
                    f"[Widget:{self.widget_id}] WidgetHook.on_create 异常，取消 hook",
                )
                self.hook = None
        _rlog.info(
            "core_service",
            f"[Widget:{self.widget_id}] load 完成 (class={self.widget_class}, agent={agent_cfg.get('type')}, context={context_cfg.get('type')})",
        )

    async def unload(self) -> None:
        """释放资源，幂等。"""
        if self._shutting_down and not self._loaded:
            return
        self._shutting_down = True
        # WidgetHook.on_destroy（Phase 11）— 在释放下层资源之前
        if self.hook is not None:
            try:
                res = self.hook.on_destroy()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                _rlog.exception("core_service", f"[Widget:{self.widget_id}] hook.on_destroy 异常")
        self.hook = None
        # 取消正在进行的 agent 任务
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except (asyncio.CancelledError, Exception):
                pass
        self._current_task = None

        # context_handler 可能持有句柄；优先调它的 close。
        if self.context_handler is not None:
            close = getattr(self.context_handler, "close", None) or getattr(
                self.context_handler, "shutdown", None
            )
            if callable(close):
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    _rlog.exception("core_service", f"[Widget:{self.widget_id}] context_handler.close 异常")
        self.context_handler = None
        self.agent = None
        # 关闭 store（Phase 3.3）
        if self.store is not None:
            try:
                await self.store.close()
            except Exception:
                _rlog.exception("core_service", f"[Widget:{self.widget_id}] store.close 异常")
        self.store = None
        self.widget_tools = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Schema-driven view (Phase: schema-driven dashboard)
    # ------------------------------------------------------------------

    def compute_view(self) -> Any:
        """Return a ViewSchema describing how this widget's detail view should render.

        Default implementation wraps compute_highlight() as a kv_table.
        Widget hooks can override by implementing compute_view() on WidgetHook.
        """
        from .view_schema import ChatLogSchema, KVTableSchema
        resolved = self.resolved_config or {}
        agent_cfg = resolved.get("agent") or {}
        context_cfg = resolved.get("context") or {}

        # Agent-capable widgets → full chat interface
        if agent_cfg.get("type"):
            return ChatLogSchema(widget_id=self.widget_id)

        # Non-agent widgets → KV table with config + live highlight rows
        highlight: dict[str, Any] = {}
        if self.hook is not None:
            try:
                result = self.hook.compute_highlight()
                if isinstance(result, dict):
                    highlight = result
            except Exception:
                pass
        cfg_rows: list = []
        if agent_cfg.get("llm"):
            cfg_rows.append(["llm_id", str(agent_cfg["llm"])])
        if context_cfg.get("type"):
            cfg_rows.append(["context_type", str(context_cfg["type"])])
        highlight_rows = [[str(k), str(v)] for k, v in highlight.items()]
        return KVTableSchema(rows=cfg_rows + highlight_rows)

    async def handle_command(self, command: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a frontend command to the widget.

        Built-in commands:
          - ``send``: send a chat message (args: {text})
          - ``stop``: cancel the running task
        Widget hooks may override by implementing handle_command().

        Returns a dict ``{ok, data?, error?}``.
        """
        # Hook override
        if self.hook is not None:
            hook_fn = getattr(self.hook, "handle_command", None)
            if callable(hook_fn):
                try:
                    result = hook_fn(command, args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    if isinstance(result, dict):
                        return result
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

        # Built-in commands
        if command == "stop":
            cancelled = self.cancel()
            return {"ok": True, "data": {"cancelled": cancelled}}

        if command == "send":
            text = args.get("text", "")
            if not text:
                return {"ok": False, "error": "args.text is required"}
            # Build a minimal task handler for fire-and-forget processing
            from ..task_manager import TaskManager
            tm = TaskManager.instance()
            task_id = f"cmd_{self.widget_id}_{datetime.now().strftime('%H%M%S%f')}"
            task = tm.create_task(
                task_id=task_id,
                description=f"cmd:send widget={self.widget_id}",
                belonging=self.belongs_to,
            )
            # Import here to avoid circular dependency
            from ..core.ps_gateway import _make_widget_task_handler
            try:
                th = _make_widget_task_handler(task, self.belongs_to)
            except Exception:
                # Fallback: use a minimal handler if helper not available
                th = task
            asyncio.create_task(
                self.process_message({"content": text}, task_handler=th)
            )
            return {"ok": True, "data": {"task_id": task_id}}

        return {"ok": False, "error": f"Unknown command: {command}"}

    # ------------------------------------------------------------------
    # 消息入口
    # ------------------------------------------------------------------

    async def process_message(
        self,
        message: dict[str, Any],
        source: Literal["chat", "cron"] = "chat",
        request_id: str | None = None,
        *,
        task_handler: Any = None,
        user_id: str | None = None,
        stream_callback: Callable[[Any], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """处理一条消息，返回响应字典。

        Args:
            message: 用户消息 dict（至少含 ``content``）
            source: 触发来源（``chat`` / ``cron``），用于打 tag
            request_id: 调用方追踪 id，会回填到响应里
            task_handler: 调用方提供的 ``WidgetTaskHandler``。Phase 1.2 强制要求
                由调用方（CoreScheduler 或 cron）创建后传入，避免 Widget
                与 ``TaskManager`` 单例耦合。
            user_id: 发送者 ID；缺省时取 ``task_handler.get_user_id()`` 或
                ``"default_user"``。
            stream_callback: 可选的流式回调 async (StreamChunk) -> None。
                由调用方（如 PSGateway）传入，用于实时推送 LLM 输出 chunk
                到前端 WebSocket。设置后会透传给 agent.process_v2()。

        Returns:
            ``{"type": "response", "ps_id", "widget_id", "content",
              "timestamp", "request_id", "source"}``
        """
        if not self._loaded:
            raise RuntimeError(
                f"Widget {self.widget_id} 未 load，无法处理消息"
            )
        if task_handler is None:
            raise ValueError(
                "Widget.process_message 需要 task_handler — "
                "请由 CoreScheduler / cron 在调用前创建"
            )

        msg_user_id = (
            user_id
            or (
                task_handler.get_user_id()
                if hasattr(task_handler, "get_user_id")
                else None
            )
            or "default_user"
        )
        user_msg: dict[str, Any] = {
            "role": "user",
            "type": "user",
            "content": message.get("content", ""),
            "user_id": msg_user_id,
            "source": source,
            "timestamp": datetime.now().isoformat(),
        }
        if "content_parts" in message:
            user_msg["content_parts"] = message["content_parts"]

        # ── 即时命令快速路径（在锁外执行，不阻塞 agent 任务）──────────────
        if user_msg["content"].startswith("/"):
            from ..command import CommandDispatcher
            if CommandDispatcher.is_instant(user_msg["content"]):
                if hasattr(self.context_handler, "set_session_task_handler"):
                    self.context_handler.set_session_task_handler(task_handler)
                instant_result = await CommandDispatcher.dispatch(
                    content=user_msg["content"],
                    context_handler=self.context_handler,
                    task_handler=task_handler,
                )
                if instant_result is not None:
                    return {
                        "type": "response",
                        "ps_id": self.ps_id,
                        "widget_id": self.widget_id,
                        "request_id": request_id,
                        "source": source,
                        "content": instant_result,
                        "timestamp": datetime.now().isoformat(),
                    }
            # 非 instant 命令继续走 _processing_lock 路径

        self._current_request_id = request_id

        async with self._processing_lock:
            try:
                await task_handler.start()
            except Exception:
                _rlog.exception("core_service", f"[Widget:{self.widget_id}] task_handler.start 异常（继续）")

            cancelled = False
            try:
                # ── Slash 命令拦截 ──────────────────────────────────────────────
                if user_msg["content"].startswith("/"):
                    from ..command import CommandDispatcher
                    if hasattr(self.context_handler, "set_session_task_handler"):
                        self.context_handler.set_session_task_handler(task_handler)
                    _cmd_result = await CommandDispatcher.dispatch(
                        content=user_msg["content"],
                        context_handler=self.context_handler,
                        task_handler=task_handler,
                    )
                    if _cmd_result is not None:
                        response_content = _cmd_result
                    elif self.agent is not None:
                        # dispatch() 返回 None 表示「不是命令」，走正常 agent 路径
                        self._current_task = asyncio.create_task(
                            self.agent.process_v2(
                                user_message=user_msg,
                                context_handler=self.context_handler,
                                session_task_handler=task_handler,
                                stream_callback=stream_callback,
                            )
                        )
                        response_content = await self._current_task
                    else:
                        response_content = (
                            f"[测试模式] {self.widget_id} 收到来自 {msg_user_id} 的消息: "
                            f"{user_msg['content']}"
                        )
                elif self.agent is not None:
                    if hasattr(self.context_handler, "set_session_task_handler"):
                        self.context_handler.set_session_task_handler(task_handler)
                    self._current_task = asyncio.create_task(
                        self.agent.process_v2(
                            user_message=user_msg,
                            context_handler=self.context_handler,
                            session_task_handler=task_handler,
                            stream_callback=stream_callback,
                        )
                    )
                    response_content = await self._current_task
                else:
                    response_content = (
                        f"[测试模式] {self.widget_id} 收到来自 {msg_user_id} 的消息: "
                        f"{user_msg['content']}"
                    )
            except asyncio.CancelledError:
                cancelled = True
                response_content = "⚠️ 任务已被取消"
            except Exception as e:
                _rlog.exception("core_service", f"[Widget:{self.widget_id}] agent.process_v2 异常")
                response_content = f"抱歉，处理失败: {e}"
            finally:
                self._current_task = None
                self._current_request_id = None

        result = {
            "type": "response",
            "ps_id": self.ps_id,
            "widget_id": self.widget_id,
            "request_id": request_id,
            "source": source,
            "content": response_content,
            "timestamp": datetime.now().isoformat(),
        }
        if cancelled:
            result["cancelled"] = True

        # WidgetHook.on_chat / on_cron（Phase 11）— 失败不影响响应
        if self.hook is not None:
            try:
                if source == self.SOURCE_CRON:
                    trigger_id = str(message.get("trigger_id") or "")
                    res = self.hook.on_cron(trigger_id, dict(user_msg), dict(result))
                else:
                    res = self.hook.on_chat(dict(user_msg), dict(result))
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                _rlog.exception("core_service", f"[Widget:{self.widget_id}] hook on_chat/on_cron 异常")

        return result

    def cancel(self) -> bool:
        """取消当前正在运行的 agent 任务（如有）。

        Returns:
            True 如果有任务被取消；False 如果没有任务在运行。
        """
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            return True
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Widget ps={self.ps_id} id={self.widget_id} "
            f"class={self.widget_class} loaded={self._loaded}>"
        )


__all__ = ["Widget"]
