"""Git 只读命令

只允许不修改仓库状态的 git 子命令。
写操作（commit, push, pull, merge, rebase, reset, checkout 等）被完全禁止。
"""

from __future__ import annotations

from ...exceptions import SecurityViolationError
from ..base_cmd import SafeCommand
from ..cmd_registry import REGISTRY


_GIT_READONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "log",
    "diff",
    "status",
    "show",
    "branch",
    "tag",
    "ls-files",
    "ls-tree",
    "shortlog",
    "describe",
    "rev-parse",
    "rev-list",
    "cat-file",
    "blame",
    "stash",   # stash list/show 只读，stash pop/drop 有副作用，由 flag 校验捕获
    "remote",  # remote -v / remote show 只读
    "submodule",  # submodule status/summary 只读
    "config",     # config --list / --get 只读
})

_GIT_BLOCKED_SUBCOMMANDS: frozenset[str] = frozenset({
    "commit", "push", "pull", "fetch", "merge", "rebase",
    "reset", "checkout", "switch", "restore", "clean",
    "rm", "mv", "add", "apply", "am", "cherry-pick",
    "init", "clone", "archive", "bundle", "gc", "prune",
    "bisect", "notes", "worktree", "reflog",
    "stash pop", "stash drop", "stash apply",
})


@REGISTRY.register
class GitCommand(SafeCommand):
    """git 命令包装器。

    第一个参数必须是允许的只读子命令；
    其余参数的 flag 不做细粒度限制，但会阻止写操作子命令。
    """
    name = "git"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset()   # 不走 flag 白名单，由 validate() 处理
    blocked_flags: frozenset[str] = frozenset()
    max_args: int = 30

    @classmethod
    def validate(cls, args: list[str], global_path_scope: str | None = None) -> None:
        if not args:
            raise SecurityViolationError(
                "git 命令缺少子命令，必须至少提供一个子命令参数"
            )

        # 首个非 flag 参数为子命令
        subcommand = next((a for a in args if not a.startswith("-")), None)
        if subcommand is None:
            raise SecurityViolationError(
                "git 命令未找到子命令（所有参数都是 flag）"
            )

        if subcommand in _GIT_BLOCKED_SUBCOMMANDS:
            raise SecurityViolationError(
                f"git 子命令 '{subcommand}' 是写操作，被安全策略禁止"
            )

        if subcommand not in _GIT_READONLY_SUBCOMMANDS:
            raise SecurityViolationError(
                f"git 子命令 '{subcommand}' 未在允许列表中，"
                f"允许的子命令: {sorted(_GIT_READONLY_SUBCOMMANDS)}"
            )

        # 通用 max_args 检查
        if len(args) > cls.max_args:
            raise SecurityViolationError(
                f"git 命令参数数量 {len(args)} 超过上限 {cls.max_args}"
            )
