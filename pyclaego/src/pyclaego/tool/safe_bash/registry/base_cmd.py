"""SafeCommand 抽象基类 — 每条 Unix 命令的安全契约"""

from __future__ import annotations

import os
from abc import ABC
from typing import ClassVar

from ....logging import get_running_log
from ..exceptions import SecurityViolationError

_rlog = get_running_log()

# Shell 元字符集合：这些字符在 shell 中有特殊含义，但通过 subprocess_exec
# 传入时已无害（不会被 shell 解释）。即便如此，我们仍主动拒绝，原因：
#   1. 提供明确的错误信息，而不是让命令以意外方式静默失败
#   2. 防止某些命令（如 eval、sh -c）将这些字符作为输入再次传给 shell
#   3. 使安全模型显式可审计
_SHELL_METACHAR_PATTERNS: tuple[str, ...] = (
    "|",    # 管道
    ";",    # 命令分隔
    "&&",   # 条件执行
    "||",   # 条件执行
    ">",    # 输出重定向
    "<",    # 输入重定向
    ">>",   # 追加重定向
    "&",    # 后台执行
    "$(",   # 命令替换
    "`",    # 命令替换（旧语法）
    "${",   # 变量展开
    "!",    # 历史展开 / 逻辑非（部分 shell）
    "\n",   # 换行（newline 注入）
    "\r",   # 回车
    "\0",   # 空字节注入
)


class SafeCommand(ABC):
    """每个注册命令的安全规格声明。

    子类通过覆盖类变量来声明自己的约束，无需重写方法。
    如需复杂的自定义验证逻辑，可覆盖 validate()。

    类变量语义：
        name           命令二进制名（如 "ls"、"grep"）
        allowed_flags  白名单：只有这些 flag 被接受（空集 = 不限制）
        blocked_flags  黑名单：这些 flag 被无条件拒绝
        max_args       允许的最大参数总数（含 flag）
        path_scope     如果非 None，所有路径类参数必须在该前缀下
    """

    name: ClassVar[str]
    allowed_flags: ClassVar[frozenset[str]] = frozenset()
    blocked_flags: ClassVar[frozenset[str]] = frozenset()
    max_args: ClassVar[int] = 20
    path_scope: ClassVar[str | None] = None

    # Path-extraction declaration used by central WorkspacePathRule.
    # ``"read"``  — every positional path-shaped arg is a read target.
    # ``"write"`` — every positional path-shaped arg is a write target.
    # ``"none"``  — this command does not touch the filesystem.
    # Commands with mixed read/write semantics (e.g. cp/mv) override
    # ``extract_paths`` directly.
    path_kind: ClassVar[str] = "none"

    @classmethod
    def extract_paths(cls, args: list[str]) -> dict[str, list[str]]:
        """Return the read/write paths this command invocation will touch.

        Default implementation interprets ``path_kind`` and treats every
        non-flag argument that looks like a filesystem path (contains '/'
        or starts with '~') as a target of that kind. Subclasses with
        more nuanced argument grammars should override this method.
        """
        out: dict[str, list[str]] = {"read": [], "write": []}
        if cls.path_kind not in ("read", "write"):
            return out
        bucket = out[cls.path_kind]
        for a in args:
            if not isinstance(a, str) or not a or a.startswith("-"):
                continue
            if "/" in a or a.startswith("~"):
                bucket.append(a)
        return out

    @classmethod
    def validate(cls, args: list[str], global_path_scope: str | None = None) -> None:
        """验证参数列表是否符合安全约束。

        检查顺序：
          1. max_args 总数检查
          2. shell 元字符检查（主动拒绝 |, ;, &&, $(), ` 等）
          3. blocked_flags 黑名单检查
          4. allowed_flags 白名单检查（若配置了白名单）
          5. 路径范围检查（per-command path_scope 或全局 global_path_scope）

        Args:
            args:              LLM 传入的参数列表
            global_path_scope: 来自 SafeBashTool 配置的全局路径限制（可覆盖）

        Raises:
            SecurityViolationError: 任何检查不通过时抛出
        """
        # 1. 参数数量上限
        if len(args) > cls.max_args:
            raise SecurityViolationError(
                f"命令 '{cls.name}' 参数数量 {len(args)} 超过上限 {cls.max_args}"
            )

        for arg in args:
            # 2. Shell 元字符检查（适用于所有参数，无论是否为 flag）
            cls._check_shell_metachar(arg)

            if not arg.startswith("-"):
                continue  # 非 flag 参数在后续路径检查中处理

            # 处理 --flag=value 形式：只取等号前的部分
            flag_key = arg.split("=")[0]

            # 3. 黑名单检查
            if flag_key in cls.blocked_flags:
                raise SecurityViolationError(
                    f"命令 '{cls.name}' 的 flag '{flag_key}' 被禁止"
                )

            # 4. 白名单检查（只在白名单非空时生效）
            if cls.allowed_flags and flag_key not in cls.allowed_flags:
                raise SecurityViolationError(
                    f"命令 '{cls.name}' 的 flag '{flag_key}' 不在允许列表中，"
                    f"允许的 flag: {sorted(cls.allowed_flags)}"
                )

        # 5. 路径范围检查
        effective_scope = cls.path_scope or global_path_scope
        if effective_scope:
            cls._check_path_scope(args, effective_scope)

    @classmethod
    def _check_shell_metachar(cls, arg: str) -> None:
        """检查单个参数是否包含 shell 元字符。

        虽然 subprocess_exec 已在执行层面消除了 shell 元字符的危害，
        此检查提供：
          - 明确的拒绝信息（而非静默失败）
          - 防止二次求值（如某命令将参数传给 sh -c）
        """
        for meta in _SHELL_METACHAR_PATTERNS:
            if meta in arg:
                raise SecurityViolationError(
                    f"命令 '{cls.name}' 的参数 {arg!r} 包含 shell 元字符 {meta!r}，"
                    "不允许在参数中使用 shell 运算符或特殊字符"
                )

    @classmethod
    def _check_path_scope(cls, args: list[str], scope: str) -> None:
        """Path-scope containment is now enforced centrally by
        ``WorkspacePathRule`` (which sees all arg semantics via
        ``extract_paths``). This in-tool check is kept as defense-in-depth
        but only logs a ``[SECURITY_GAP]`` warning instead of raising.
        """
        norm_scope = os.path.normpath(os.path.expanduser(scope))
        for arg in args:
            if arg.startswith("-"):
                continue
            # 启发式：含路径分隔符或以 ~ 开头则视为路径参数
            if "/" not in arg and not arg.startswith("~"):
                continue
            expanded = os.path.normpath(os.path.expanduser(arg))
            if not expanded.startswith(norm_scope):
                _rlog.warning(
                    "core_service",
                    f"[SECURITY_GAP] safe_bash '{cls.name}': path arg {arg!r} "
                    f"escapes configured path_scope {scope!r} but in-tool check "
                    f"no longer blocks; central WorkspacePathRule should reject "
                    f"if applicable.",
                )
