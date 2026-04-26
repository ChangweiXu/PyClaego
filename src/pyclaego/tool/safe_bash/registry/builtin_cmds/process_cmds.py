"""进程查看命令（ps / pgrep / kill）

ps / pgrep 为只读进程查询；
kill 被禁止（进程终止属于破坏性操作）。
"""

from __future__ import annotations

from ..base_cmd import SafeCommand
from ..cmd_registry import REGISTRY


@REGISTRY.register
class PsCommand(SafeCommand):
    """ps — 进程状态查看"""
    name = "ps"
    allowed_flags: frozenset[str] = frozenset({
        "-e", "-a", "-A", "-u", "-f", "-l", "-o",
        "--pid", "--ppid", "--user", "--group",
        "-p", "-C", "-t",
        "aux", "auxf",  # 常见复合选项（ps aux 不带连字符）
    })
    blocked_flags: frozenset[str] = frozenset()
    max_args: int = 15


@REGISTRY.register
class PgrepCommand(SafeCommand):
    """pgrep — 按名称查找进程 PID"""
    name = "pgrep"
    allowed_flags: frozenset[str] = frozenset({
        "-l", "--list-name",
        "-a", "--list-full",
        "-u", "--uid",
        "-g", "--gid",
        "-x", "--exact",
        "-i", "--ignore-case",
        "-n", "--newest",
        "-o", "--oldest",
        "-c", "--count",
        "-f", "--full",
        "-t", "--terminal",
    })
    blocked_flags: frozenset[str] = frozenset()
    max_args: int = 10
