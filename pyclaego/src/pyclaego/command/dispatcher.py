"""CommandDispatcher — 服务端 slash 命令路由器。

将 `/cmd arg1 arg2 ...` 路由到 context_handler 上对应的异步方法。
新命令只需在 COMMAND_REGISTRY 中添加一行，不需要修改任何其他文件。

注册表格式：
    slash_name → (context_method, args_extractor_fn)

    context_method:    context_handler 上对应的方法名（str）
    args_extractor_fn: Callable[[list[str]], dict]，从位置参数列表构建 kwargs
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# ---------------------------------------------------------------------------
# 参数提取工具
# ---------------------------------------------------------------------------

def _no_args(_: list[str]) -> dict[str, Any]:
    return {}


def _first_arg(name: str) -> Callable[[list[str]], dict[str, Any]]:
    def _extract(args: list[str]) -> dict[str, Any]:
        return {name: args[0] if args else ""}
    return _extract


def _rest_as_str(name: str) -> Callable[[list[str]], dict[str, Any]]:
    def _extract(args: list[str]) -> dict[str, Any]:
        return {name: " ".join(args)}
    return _extract


def _passthrough_list(name: str) -> Callable[[list[str]], dict[str, Any]]:
    def _extract(args: list[str]) -> dict[str, Any]:
        return {name: args}
    return _extract


# ---------------------------------------------------------------------------
# 命令注册表
# ---------------------------------------------------------------------------

# 可在 _processing_lock 外立即执行的命令（只读 / 纯内存写）
# 写内存系统或会触发 I/O flush 的命令不应出现在此集合中
INSTANT_COMMANDS: frozenset = frozenset({
    "/memories",
    "/why",
    "/pin",
    "/unpin",
    "/export_memory",
})

# 格式：slash_name → (context_method_name, args_extractor_fn)
COMMAND_REGISTRY: dict[str, tuple[str, Callable[[list[str]], dict[str, Any]]]] = {
    # ── SoulV5 / SoulV6 共用 ───────────────────────────────────────────
    "/compress":        ("force_compress",       _no_args),
    "/rebuild_index":   ("rebuild_memory_index", _no_args),

    # ── SoulV6 专属 ────────────────────────────────────────────────────
    "/pin":             ("cmd_pin",          _first_arg("tool_call_id")),
    "/unpin":           ("cmd_unpin",        _first_arg("tool_call_id")),
    "/close_loop":      ("cmd_close_loop",   _rest_as_str("query")),
    "/memories":        ("cmd_memories",     _passthrough_list("args")),
    "/forget":          ("cmd_forget",       _first_arg("md_path")),
    "/why":             ("cmd_why",          _rest_as_str("query")),
    "/export_memory":   ("cmd_export_memory", _first_arg("target_dir")),
}

# ---------------------------------------------------------------------------
# 全局命令注册表（不依赖 context_handler，直接调用独立异步函数）
# 格式：slash_name → async callable(args: List[str]) -> str
# ---------------------------------------------------------------------------

def _load_global_registry() -> dict[str, Callable[[list[str]], Awaitable[str]]]:
    """延迟加载，避免循环导入。"""
    from .cron_handlers import handle_cron
    from .llm_handlers import handle_llm
    return {
        "/cron": handle_cron,
        "/llm": handle_llm,
    }


GLOBAL_COMMAND_REGISTRY: dict[str, Callable[[list[str]], Awaitable[str]]] = (
    _load_global_registry()
)

_HELP_HEADER = "🛠 可用命令（由服务端处理）："


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class CommandDispatcher:
    """无状态分发器 — 所有方法均为 class-level。"""

    @classmethod
    def is_instant(cls, content: str) -> bool:
        """返回 True 如果 content 是一条可在 _processing_lock 外立即执行的命令。

        ``/help`` 也视为 instant（纯字符串拼接，无 I/O）。
        """
        if not content.startswith("/"):
            return False
        cmd = content.split(maxsplit=1)[0].lower()
        return cmd in INSTANT_COMMANDS or cmd == "/help"

    @classmethod
    async def dispatch(
        cls,
        content: str,
        context_handler: Any,
        task_handler: Any,
    ) -> str | None:
        """解析并执行 slash 命令。

        Args:
            content:         用户消息内容（原始字符串）
            context_handler: widget 的 context_handler 实例
            task_handler:    当前 WidgetTaskHandler（供命令创建子任务使用）

        Returns:
            命令响应文本（已执行）；
            ``None`` 表示 content 不以 ``/`` 开头，不是命令，调用方应走正常 agent 流程。
        """
        if not content.startswith("/"):
            return None

        parts = content.split(maxsplit=1)
        cmd = parts[0].lower()
        raw_arg = parts[1].strip() if len(parts) > 1 else ""
        args = raw_arg.split() if raw_arg else []

        # 内建命令
        if cmd == "/help":
            return cls._build_help(context_handler)

        # 全局命令注册表（不依赖 context_handler）
        global_fn = GLOBAL_COMMAND_REGISTRY.get(cmd)
        if global_fn is not None:
            try:
                result = await global_fn(args)
                return result if isinstance(result, str) else str(result)
            except Exception as exc:
                return f"❌ 命令 {cmd} 执行失败: {exc}"

        # context 命令注册表查找
        entry = COMMAND_REGISTRY.get(cmd)
        if entry is None:
            available = [c for c in COMMAND_REGISTRY if hasattr(context_handler, COMMAND_REGISTRY[c][0])]
            return (
                f"❌ 未知命令: {cmd}\n"
                f"输入 /help 查看可用命令（当前 context 支持 {len(available)} 条）"
            )

        method_name, extract_args = entry
        method = getattr(context_handler, method_name, None)
        if method is None:
            return (
                f"⚠️ 命令 {cmd} 在当前 context 类型（"
                f"{type(context_handler).__name__}）中不可用"
            )

        try:
            kwargs = extract_args(args)
            result = await method(**kwargs)
            return result if isinstance(result, str) else str(result)
        except Exception as exc:
            return f"❌ 命令 {cmd} 执行失败: {exc}"

    @classmethod
    def _build_help(cls, context_handler: Any) -> str:
        lines = [_HELP_HEADER, ""]
        # 全局命令（始终可用）
        for slash in GLOBAL_COMMAND_REGISTRY:
            lines.append(f"  {slash}")
        # context 命令（依赖 context_handler 方法）
        for slash, (method_name, _) in COMMAND_REGISTRY.items():
            available = hasattr(context_handler, method_name)
            marker = "  " if available else "  [不可用] "
            lines.append(f"{marker}{slash}")
        lines += [
            "",
            "  /help       显示此帮助",
            "",
            "注：/stop 通过 control 帧发送，不走此路径",
        ]
        return "\n".join(lines)
