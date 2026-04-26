"""Session 命令处理器"""

import asyncio
from typing import Dict, Any, Optional, Callable, Awaitable
from datetime import datetime
from ..logging import get_running_log

_rlog = get_running_log()


class CommandHandler:
    """Session 命令处理器
    
    负责解析和执行用户指令（以 / 开头的消息）

    命令分为两类：
    - 立即命令（IMMEDIATE_COMMANDS）：/stop、/help，收到即执行，不入队列
    - 队列命令（QUEUED_COMMANDS）：/compress，入队等待当前消息处理完毕后串行执行
    """

    # 立即执行命令集合（不入队，收到即执行）
    IMMEDIATE_COMMANDS = {
        "stop", "help", "llm", "cron",
        # SoulV6 命令
        "pin", "unpin", "close_loop", "memories", "forget", "why", "export_memory",
    }

    # 队列命令集合（入队，等待当前消息处理完毕后执行）
    QUEUED_COMMANDS = {"compress"}
    
    def __init__(self, session):
        """初始化命令处理器
        
        Args:
            session: Session 实例引用
        """
        self.session = session
        
        # 注册命令处理函数
        self.commands: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {
            "stop": self._handle_stop,
            "help": self._handle_help,
            "compress": self._handle_compress,
            "llm": self._handle_llm,
            "rebuild_memory_index": self._handle_rebuild_memory_index,
            "cron": self._handle_cron,
            # SoulV6 命令
            "pin": self._handle_v6_pin,
            "unpin": self._handle_v6_unpin,
            "close_loop": self._handle_v6_close_loop,
            "memories": self._handle_v6_memories,
            "forget": self._handle_v6_forget,
            "why": self._handle_v6_why,
            "export_memory": self._handle_v6_export_memory,
        }
    
    @staticmethod
    def is_queued_command(message: Dict[str, Any]) -> bool:
        """判断命令是否为队列命令（需要等待当前消息处理完毕才能执行）

        Args:
            message: 消息字典

        Returns:
            是否为队列命令
        """
        content = message.get("content", "").strip()
        if not content.startswith("/"):
            return False
        command_name = content[1:].split()[0].lower() if content[1:] else ""
        return command_name in CommandHandler.QUEUED_COMMANDS

    @staticmethod
    def is_command(message: Dict[str, Any]) -> bool:
        """判断消息是否为命令
        
        Args:
            message: 消息字典
            
        Returns:
            是否为命令
        """
        content = message.get("content", "").strip()
        return content.startswith("/")
    
    @staticmethod
    def parse_command(message: Dict[str, Any]) -> tuple:
        """解析命令
        
        Args:
            message: 消息字典
            
        Returns:
            (命令名, 参数列表)
        """
        content = message.get("content", "").strip()
        if not content.startswith("/"):
            return "", []
        
        # 去掉前导 /
        command_line = content[1:]
        
        # 分割命令和参数
        parts = command_line.split()
        command_name = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []
        
        return command_name, args
    
    async def handle_command(
        self, 
        message: Dict[str, Any], 
        user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """处理命令
        
        Args:
            message: 消息字典
            user_id: 用户ID
            
        Returns:
            响应消息
        """
        command_name, args = self.parse_command(message)
        
        # 检查命令是否存在
        if command_name not in self.commands:
            return {
                "type": "command_response",
                "session_id": self.session.session_id,
                "content": f"❌ 未知命令: /{command_name}\n💡 使用 /help 查看可用命令",
                "timestamp": datetime.now().isoformat(),
                "command": command_name,
                "success": False
            }
        
        # 执行命令
        handler = self.commands[command_name]
        return await handler({
            "command": command_name,
            "args": args,
            "user_id": user_id,
            "original_message": message
        })
    
    async def _handle_stop(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """处理 /stop 命令
        
        终止当前任务并清空消息队列
        """
        _rlog.info(f"session_{self.session.session_id}", f"[CommandHandler] 执行 /stop 命令")
        
        status_parts = []
        
        # 1. 取消当前正在执行的任务
        if self.session._current_agent_task and not self.session._current_agent_task.done():
            self.session._current_agent_task.cancel()
            status_parts.append("✓ 已请求取消当前任务")
            _rlog.info(f"session_{self.session.session_id}", "[CommandHandler] 已调用 task.cancel()")
        else:
            status_parts.append("• 当前没有正在执行的任务")
        
        # 2. 清空消息队列
        cleared_count = 0
        while not self.session._message_queue.empty():
            try:
                # 取出队列 item（dict 格式）并向 future 设置异常
                item = self.session._message_queue.get_nowait()
                if item is not None:
                    response_future = item["response_future"]
                    if not response_future.done():
                        response_future.set_exception(
                            Exception("任务已被 /stop 命令取消")
                        )
                    cleared_count += 1
                    self.session._message_queue.task_done()
            except asyncio.QueueEmpty:
                break
        
        if cleared_count > 0:
            status_parts.append(f"✓ 已清空消息队列（{cleared_count} 条消息）")
        else:
            status_parts.append("• 消息队列已为空")
        
        content = "🛑 停止命令执行结果:\n" + "\n".join(status_parts)
        
        return {
            "type": "command_response",
            "session_id": self.session.session_id,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "command": "stop",
            "success": True,
            "metadata": {
                "was_processing": self.session._is_processing,
                "cleared_messages": cleared_count
            }
        }
    
    async def _handle_compress(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """处理 /compress 命令

        强制对当前短期记忆执行常态化截断（V5 专用）。
        支持可选参数 --llm：截断后额外调用 LLM 对当前消息进行知识提炼（预留）。

        用法：
          /compress
          /compress --llm
        """
        args = context.get("args", [])
        use_llm = "--llm" in args

        _rlog.info(
            f"session_{self.session.session_id}",
            f"[CommandHandler] 执行 /compress 命令 (use_llm={use_llm})",
        )

        context_handler = self.session.context_handler
        # 检查 context_handler 是否支持 force_compress
        if not hasattr(context_handler, "force_compress"):
            return {
                "type": "command_response",
                "session_id": self.session.session_id,
                "content": "❌ 当前 Context 策略不支持 /compress 命令（需要 soul_v5 策略）",
                "timestamp": datetime.now().isoformat(),
                "command": "compress",
                "success": False,
            }

        try:
            result_msg = await context_handler.force_compress(use_llm=use_llm)
        except Exception as e:
            import traceback
            _rlog.error(
                f"session_{self.session.session_id}",
                f"[CommandHandler] /compress 执行失败: {e}\n{traceback.format_exc()}",
            )
            return {
                "type": "command_response",
                "session_id": self.session.session_id,
                "content": f"❌ /compress 执行失败: {str(e)}",
                "timestamp": datetime.now().isoformat(),
                "command": "compress",
                "success": False,
            }

        return {
            "type": "command_response",
            "session_id": self.session.session_id,
            "content": result_msg,
            "timestamp": datetime.now().isoformat(),
            "command": "compress",
            "success": True,
        }

    async def _handle_help(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """处理 /help 命令
        
        显示可用命令列表
        """
        help_text = """📖 可用命令列表:

/stop
  🛑 终止当前任务并清空消息队列
  使用场景: Agent 执行时间过长或陷入错误状态时

/compress [--llm]
  ✂️  强制对当前短期记忆执行常态化截断（仅 soul_v5 策略有效）
  --llm: 截断后额外调用 LLM 进行知识提炼（预留功能）

/rebuild_memory_index
  🔄 从 MD 文件树重建 SQLite 索引（仅 soul_v5 策略有效）

/llm [provider_id]
  🤖 查看当前 session 的 LLM 设置（provider、model、temperature 等）
  provider_id: 可选，切换到指定 provider（仅影响本次 session 运行时）

/cron [子命令]
  📅 管理 session 定时任务（需在 config.yaml 中配置 cron.enabled: true）
  list              列出所有任务及下次触发时间（默认）
  next              按触发时间排序显示
  pause  <name>     暂停指定任务（重启后失效）
  resume <name>     恢复已暂停的任务
  run    <name>     立即触发一次（不受 schedule 限制）
  help              显示 /cron 子命令帮助

/help
  💡 显示此帮助信息
"""
        
        return {
            "type": "command_response",
            "session_id": self.session.session_id,
            "content": help_text,
            "timestamp": datetime.now().isoformat(),
            "command": "help",
            "success": True
        }

    def _cmd_resp(self, command: str, content: str, success: bool) -> Dict[str, Any]:
        """构造命令响应 dict（消除各 _handle_* 方法中的重复代码）"""
        return {
            "type": "command_response",
            "session_id": self.session.session_id,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "command": command,
            "success": success,
        }

    async def _handle_rebuild_memory_index(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """处理 /rebuild_memory_index 命令

        从 MD 文件树重建 SQLite 索引（仅 soul_v5 策略有效）。
        """
        context_handler = self.session.context_handler
        if not hasattr(context_handler, "rebuild_memory_index"):
            return self._cmd_resp(
                "rebuild_memory_index",
                "❌ 当前 Context 策略不支持 /rebuild_memory_index 命令（需要 soul_v5 策略）",
                False,
            )

        try:
            result_msg = await context_handler.rebuild_memory_index()
        except Exception as e:
            import traceback
            _rlog.error(
                f"session_{self.session.session_id}",
                f"[CommandHandler] /rebuild_memory_index 执行失败: {e}\n{traceback.format_exc()}",
            )
            return self._cmd_resp(
                "rebuild_memory_index",
                f"❌ /rebuild_memory_index 执行失败: {str(e)}",
                False,
            )

        return self._cmd_resp("rebuild_memory_index", result_msg, True)

    async def _handle_llm(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """处理 /llm 命令

        无参数：显示当前 agent 使用的 llm_id 及 provider 配置详情（屏蔽 api_key）。
        有参数：动态切换当前 session 的 llm_id（仅运行时生效，不写磁盘）。

        用法：
          /llm
          /llm <provider_id>
        """
        from ..config import get_config

        args = context.get("args", [])
        config = get_config()
        llm_section = config.get("llm", {}) or {}
        providers: dict = llm_section.get("providers", {}) or {}
        available = list(providers.keys())
        agent = self.session.agent

        # ── 查看模式（无参数）──────────────────────────────────────────────
        if not args:
            if agent is None or not hasattr(agent, "get_llm_id"):
                return self._cmd_resp(
                    "llm", "❌ 当前 Session 未配置 Agent，无法查看 LLM 设置", False
                )

            llm_id: str = agent.get_llm_id() or "(未设置)"
            provider_cfg: dict = providers.get(llm_id, {})

            # 屏蔽含 key 字眼的字段
            display_cfg = {
                k: ("***MASKED***" if "key" in k.lower() else v)
                for k, v in provider_cfg.items()
            }

            lines = [f"🤖 当前 LLM 配置 (session: {self.session.session_id}):"]
            lines.append(f"  llm_id: {llm_id}")
            if display_cfg:
                lines.append("  provider 详情:")
                for k, v in display_cfg.items():
                    lines.append(f"    {k:<20} {v}")
            else:
                lines.append(f"  ⚠️  provider '{llm_id}' 不在配置文件的 llm.providers 中")

            lines.append(f"\n可用 providers: {', '.join(available) if available else '(无)'}")
            return self._cmd_resp("llm", "\n".join(lines), True)

        # ── 切换模式（有参数）──────────────────────────────────────────────
        new_provider = args[0]

        if new_provider not in providers:
            return self._cmd_resp(
                "llm",
                f"❌ provider 不存在: {new_provider}\n"
                f"💡 可用 providers: {', '.join(available) if available else '(无)'}",
                False,
            )

        if agent is None or not hasattr(agent, "set_llm_id"):
            return self._cmd_resp(
                "llm", "❌ 当前 Session 未配置 Agent，无法切换 LLM", False
            )

        old_provider = agent.get_llm_id()
        agent.set_llm_id(new_provider)
        new_cfg = providers[new_provider]

        _rlog.info(
            f"session_{self.session.session_id}",
            f"[CommandHandler] /llm 切换: {old_provider} → {new_provider}",
        )

        return self._cmd_resp(
            "llm",
            f"✅ LLM 已切换: {old_provider} → {new_provider}\n"
            f"  model:   {new_cfg.get('model', 'N/A')}\n"
            f"  api:     {new_cfg.get('api', 'N/A')}",
            True,
        )

    async def _handle_cron(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """处理 /cron 命令

        子命令：
          /cron list                  显示所有 cron 任务
          /cron next                  按下次触发时间排序显示
          /cron pause <name>          暂停指定任务（仅内存态）
          /cron resume <name>         恢复指定任务
          /cron run <name>            立即触发一次
          /cron help                  显示帮助
        """
        args = context.get("args", []) or []
        cron = getattr(self.session, "cron", None)

        if cron is None:
            return self._cmd_resp(
                "cron",
                "❌ 当前 Session 未启用 cron（在 <workspace>/config.yaml 添加 cron.enabled: true）",
                False,
            )

        sub = (args[0] if args else "list").lower()
        rest = args[1:]

        if sub == "help":
            return self._cmd_resp(
                "cron",
                "📅 /cron 子命令:\n"
                "  /cron list             列出所有任务\n"
                "  /cron next             按下次触发时间排序\n"
                "  /cron pause <name>     暂停任务（重启后失效）\n"
                "  /cron resume <name>    恢复任务\n"
                "  /cron run <name>       立即触发一次\n",
                True,
            )

        if sub in ("list", "next"):
            jobs = cron.list_jobs()
            if sub == "next":
                jobs.sort(key=lambda j: (j["next_fire_time"] is None, j["next_fire_time"] or ""))
            if not jobs:
                return self._cmd_resp("cron", "📅 当前没有定义任何 cron 任务", True)
            lines = [f"📅 Cron 任务 (timezone={cron.timezone}):"]
            for j in jobs:
                flags = []
                if not j["enabled"]:
                    flags.append("disabled")
                if j["paused"]:
                    flags.append("paused")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(
                    f"  • {j['name']:<20} {j['schedule']:<15} next={j['next_fire_time'] or '-'}{flag_str}\n"
                    f"    {j['prompt_preview']}"
                )
            return self._cmd_resp("cron", "\n".join(lines), True)

        if sub in ("pause", "resume", "run"):
            if not rest:
                return self._cmd_resp(
                    "cron", f"❌ /cron {sub} 需要任务名称参数", False
                )
            name = rest[0]
            if sub == "pause":
                ok = cron.pause(name)
                msg = f"⏸  已暂停: {name}" if ok else f"❌ 任务不存在: {name}"
                return self._cmd_resp("cron", msg, ok)
            if sub == "resume":
                ok = cron.resume(name)
                msg = f"▶️  已恢复: {name}" if ok else f"❌ 任务不存在: {name}"
                return self._cmd_resp("cron", msg, ok)
            # run
            try:
                ok = await cron.run_now(name)
            except Exception as e:
                return self._cmd_resp("cron", f"❌ 触发失败: {e}", False)
            msg = f"🚀 已入队执行: {name}" if ok else f"❌ 任务不存在: {name}"
            return self._cmd_resp("cron", msg, ok)

        return self._cmd_resp(
            "cron",
            f"❌ 未知子命令: {sub}\n💡 使用 /cron help 查看帮助",
            False,
        )

    # ==================================================================
    # SoulV6 命令（要求 context_handler 是 SoulV6ContextHandler）
    # ==================================================================

    def _v6_handler(self):
        """返回 SoulV6 handler 或 None"""
        ch = self.session.context_handler
        if ch is None:
            return None
        # 鸭子类型：检查是否有 V6 命令方法
        if hasattr(ch, "cmd_pin") and hasattr(ch, "cmd_close_loop"):
            return ch
        return None

    def _v6_unsupported(self, cmd: str) -> Dict[str, Any]:
        return self._cmd_resp(
            cmd,
            f"❌ 当前 Context 策略不支持 /{cmd} 命令（需要 soul_v6 策略）",
            False,
        )

    async def _handle_v6_pin(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("pin")
        args = context.get("args", []) or []
        msg = await h.cmd_pin(args[0] if args else "")
        return self._cmd_resp("pin", msg, msg.startswith("✅"))

    async def _handle_v6_unpin(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("unpin")
        args = context.get("args", []) or []
        msg = await h.cmd_unpin(args[0] if args else "")
        return self._cmd_resp("unpin", msg, msg.startswith("✅"))

    async def _handle_v6_close_loop(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("close_loop")
        args = context.get("args", []) or []
        query = " ".join(args)
        msg = await h.cmd_close_loop(query)
        return self._cmd_resp("close_loop", msg, msg.startswith("✅"))

    async def _handle_v6_memories(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("memories")
        msg = await h.cmd_memories(context.get("args", []) or [])
        return self._cmd_resp("memories", msg, True)

    async def _handle_v6_forget(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("forget")
        args = context.get("args", []) or []
        msg = await h.cmd_forget(args[0] if args else "")
        return self._cmd_resp("forget", msg, msg.startswith("✅"))

    async def _handle_v6_why(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("why")
        args = context.get("args", []) or []
        msg = await h.cmd_why(" ".join(args))
        return self._cmd_resp("why", msg, True)

    async def _handle_v6_export_memory(self, context: Dict[str, Any]) -> Dict[str, Any]:
        h = self._v6_handler()
        if h is None:
            return self._v6_unsupported("export_memory")
        args = context.get("args", []) or []
        msg = await h.cmd_export_memory(args[0] if args else "")
        return self._cmd_resp("export_memory", msg, msg.startswith("✅"))
