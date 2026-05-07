"""命令注册表 — 维护 name → SafeCommand 的映射，并提供树级验证"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..exceptions import UnknownCommandError
from ..tree.nodes import AndNode, CmdNode, Node, OrNode, PipelineNode, SeqNode
from .base_cmd import SafeCommand

if TYPE_CHECKING:
    pass


class CommandRegistry:
    """命令注册表单例。

    使用方式：
        @REGISTRY.register
        class LsCommand(SafeCommand):
            name = "ls"
            ...
    """

    def __init__(self) -> None:
        self._cmds: dict[str, type[SafeCommand]] = {}

    def register(self, cmd_class: type[SafeCommand]) -> type[SafeCommand]:
        """将命令类注册到注册表（可用作装饰器）。

        Args:
            cmd_class: SafeCommand 子类

        Returns:
            原始 cmd_class（装饰器语义）
        """
        name = getattr(cmd_class, "name", None)
        if not name:
            raise ValueError(f"{cmd_class.__name__} 未定义 'name' 类变量")
        self._cmds[name] = cmd_class
        return cmd_class

    def get(self, name: str) -> type[SafeCommand]:
        """按命令名查找命令类。

        Args:
            name: 命令二进制名（如 "ls"）

        Returns:
            对应的 SafeCommand 子类

        Raises:
            UnknownCommandError: 命令未注册
        """
        cmd_class = self._cmds.get(name)
        if cmd_class is None:
            raise UnknownCommandError(
                f"命令 '{name}' 未在安全注册表中注册，"
                f"已注册的命令: {sorted(self._cmds.keys())}"
            )
        return cmd_class

    def validate_tree(self, node: Node, path_scope: str | None = None) -> None:
        """遍历命令树，对每个 CmdNode 执行注册表查找和安全验证。

        Args:
            node:       命令树根节点
            path_scope: 全局路径限制（透传给 SafeCommand.validate）

        Raises:
            UnknownCommandError:   命令未注册
            SecurityViolationError: 参数违反安全策略
        """
        self._walk(node, path_scope)

    def list_commands(self) -> list[str]:
        """返回所有已注册命令名的有序列表。"""
        return sorted(self._cmds.keys())

    def extract_paths_from_tree(self, node: Node) -> dict[str, list[str]]:
        """Walk ``node`` and aggregate the read/write paths every CmdNode declares.

        Unknown commands contribute nothing (they will be rejected during
        ``validate_tree``); callers that want to enforce path policy at
        the security-rule layer should call ``validate_tree`` separately.
        Redirection targets (``stdin``/``stdout``) on each ``CmdNode`` are
        merged into the result.
        """
        out: dict[str, list[str]] = {"read": [], "write": []}
        self._walk_paths(node, out)
        return out

    def _walk_paths(self, node: Node, out: dict[str, list[str]]) -> None:
        if isinstance(node, CmdNode):
            cmd_class = self._cmds.get(node.name)
            if cmd_class is not None:
                contrib = cmd_class.extract_paths(node.args)
                out["read"].extend(contrib.get("read", []))
                out["write"].extend(contrib.get("write", []))
            if node.stdin and node.stdin.file:
                out["read"].append(node.stdin.file)
            if node.stdout and node.stdout.file:
                out["write"].append(node.stdout.file)
            return

        if isinstance(node, PipelineNode):
            for step in node.steps:
                self._walk_paths(step, out)
            return

        if isinstance(node, SeqNode):
            for step in node.steps:
                self._walk_paths(step, out)
            return

        if isinstance(node, (AndNode, OrNode)):
            self._walk_paths(node.left, out)
            self._walk_paths(node.right, out)
            return

    # ------------------------------------------------------------------
    # 内部遍历
    # ------------------------------------------------------------------

    def _walk(self, node: Node, path_scope: str | None) -> None:
        if isinstance(node, CmdNode):
            cmd_class = self.get(node.name)
            cmd_class.validate(node.args, global_path_scope=path_scope)
            return

        if isinstance(node, PipelineNode):
            for step in node.steps:
                self._walk(step, path_scope)
            return

        if isinstance(node, SeqNode):
            for step in node.steps:
                self._walk(step, path_scope)
            return

        if isinstance(node, (AndNode, OrNode)):
            self._walk(node.left, path_scope)
            self._walk(node.right, path_scope)
            return


# 模块级单例 — 所有 builtin_cmds 通过 @REGISTRY.register 注册
REGISTRY = CommandRegistry()
