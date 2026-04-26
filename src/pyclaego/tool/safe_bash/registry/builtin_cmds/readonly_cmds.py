"""只读类内置命令

包含：ls, cat, head, tail, grep, find, wc, stat, du, file,
      echo, sort, uniq, cut, tr, pwd, date, which, env, test
"""

from __future__ import annotations

from ..base_cmd import SafeCommand
from ..cmd_registry import REGISTRY


@REGISTRY.register
class LsCommand(SafeCommand):
    name = "ls"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset()  # 不限制 flag
    blocked_flags: frozenset[str] = frozenset()


@REGISTRY.register
class CatCommand(SafeCommand):
    name = "cat"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({"-n", "-b", "-A", "-v", "-e", "-t"})


@REGISTRY.register
class HeadCommand(SafeCommand):
    name = "head"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({"-n", "-c", "-q", "-v"})


@REGISTRY.register
class TailCommand(SafeCommand):
    name = "tail"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({"-n", "-c", "-f", "-F", "-q", "-v", "--follow"})


@REGISTRY.register
class GrepCommand(SafeCommand):
    name = "grep"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({
        "-r", "--recursive",
        "-i", "--ignore-case",
        "-n", "--line-number",
        "-l", "--files-with-matches",
        "-L", "--files-without-match",
        "-v", "--invert-match",
        "-e", "--regexp",
        "-E", "--extended-regexp",
        "-F", "--fixed-strings",
        "-P", "--perl-regexp",
        "--include", "--exclude", "--exclude-dir",
        "-c", "--count",
        "-m", "--max-count",
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "--color", "--colour",
        "-o", "--only-matching",
        "-q", "--quiet", "--silent",
        "-w", "--word-regexp",
        "-x", "--line-regexp",
        "-h", "--no-filename",
        "-H", "--with-filename",
    })


@REGISTRY.register
class FindCommand(SafeCommand):
    name = "find"
    path_kind = "read"
    # -exec / -execdir / -delete / -ok 使 find 具有写入和执行能力，全部封禁
    blocked_flags: frozenset[str] = frozenset({
        "-exec", "-execdir", "-delete", "-ok", "-okdir",
    })


@REGISTRY.register
class WcCommand(SafeCommand):
    name = "wc"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({"-l", "-w", "-c", "-m", "-L"})


@REGISTRY.register
class StatCommand(SafeCommand):
    name = "stat"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({"-c", "--format", "-f", "--file-system",
                                               "-L", "--dereference", "-t", "--terse"})


@REGISTRY.register
class DuCommand(SafeCommand):
    name = "du"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({
        "-s", "--summarize", "-h", "--human-readable",
        "-a", "--all", "-c", "--total",
        "-d", "--max-depth", "--max-depth",
        "-k", "-m", "-b", "-B",
    })


@REGISTRY.register
class FileCommand(SafeCommand):
    name = "file"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({"-b", "-i", "-k", "-L", "-z", "-s"})


@REGISTRY.register
class EchoCommand(SafeCommand):
    name = "echo"
    allowed_flags: frozenset[str] = frozenset({"-n", "-e", "-E"})
    max_args: int = 50  # echo 可能有较多文本参数


@REGISTRY.register
class SortCommand(SafeCommand):
    name = "sort"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({
        "-r", "--reverse", "-n", "--numeric-sort",
        "-k", "--key", "-t", "--field-separator",
        "-u", "--unique", "-f", "--ignore-case",
        "-h", "--human-numeric-sort",
        "-V", "--version-sort",
    })


@REGISTRY.register
class UniqCommand(SafeCommand):
    name = "uniq"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({
        "-c", "--count", "-d", "--repeated",
        "-u", "--unique", "-i", "--ignore-case",
        "-f", "--skip-fields", "-s", "--skip-chars",
        "-w", "--check-chars",
    })


@REGISTRY.register
class CutCommand(SafeCommand):
    name = "cut"
    path_kind = "read"
    allowed_flags: frozenset[str] = frozenset({
        "-b", "--bytes", "-c", "--characters",
        "-f", "--fields", "-d", "--delimiter",
        "--complement", "-s", "--only-delimited",
    })


@REGISTRY.register
class TrCommand(SafeCommand):
    name = "tr"
    allowed_flags: frozenset[str] = frozenset({"-d", "-s", "-c", "-C"})


@REGISTRY.register
class PwdCommand(SafeCommand):
    name = "pwd"
    allowed_flags: frozenset[str] = frozenset({"-L", "-P"})
    max_args: int = 2


@REGISTRY.register
class DateCommand(SafeCommand):
    name = "date"
    allowed_flags: frozenset[str] = frozenset({"-u", "--utc", "--universal",
                                               "-d", "--date", "-r", "--reference",
                                               "-R", "--rfc-email", "--iso-8601",
                                               "--rfc-3339"})


@REGISTRY.register
class WhichCommand(SafeCommand):
    name = "which"
    allowed_flags: frozenset[str] = frozenset({"-a", "--all"})
    max_args: int = 5


@REGISTRY.register
class EnvCommand(SafeCommand):
    name = "env"
    # -i 会清空当前环境变量，可能导致后续命令行为异常
    blocked_flags: frozenset[str] = frozenset({"-i", "--ignore-environment"})


@REGISTRY.register
class TestCommand(SafeCommand):
    """test / [ 命令，常用于 <and>/<or> 条件判断"""
    name = "test"
    allowed_flags: frozenset[str] = frozenset()  # test 使用的都是操作符，不限制
    max_args: int = 10
