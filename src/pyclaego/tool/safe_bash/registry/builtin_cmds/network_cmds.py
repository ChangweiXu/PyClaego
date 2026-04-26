"""网络命令（curl / wget）

只允许下载和检查操作；
上传、发送请求体数据等 flag 被封禁。
"""

from __future__ import annotations

from ..base_cmd import SafeCommand
from ..cmd_registry import REGISTRY


def _collect_flag_value_targets(args: list[str], flags_with_value: set[str]) -> list[str]:
    """Return the argument values that follow any of ``flags_with_value``.

    Handles both ``-o file`` and ``--output=file`` forms.
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if not isinstance(a, str):
            i += 1
            continue
        if "=" in a and a.startswith("-"):
            key, _, val = a.partition("=")
            if key in flags_with_value and val:
                out.append(val)
        elif a in flags_with_value:
            if i + 1 < len(args):
                out.append(args[i + 1])
                i += 1
        i += 1
    return out


@REGISTRY.register
class CurlCommand(SafeCommand):
    """curl — 只允许下载和 HTTP 检查相关操作"""
    name = "curl"
    allowed_flags: frozenset[str] = frozenset({
        # 基础输出控制
        "-s", "--silent",
        "-S", "--show-error",
        "-v", "--verbose",
        "-i", "--include",
        "-I", "--head",
        # 输出目标
        "-o", "--output",
        "-O", "--remote-name",
        # 跟随重定向
        "-L", "--location",
        # 请求方法（允许 GET/HEAD，下游 flag 内容是否安全由 blocked_flags 保证）
        "-X", "--request",
        # 超时
        "--max-time",
        "--connect-timeout",
        # 请求头（读取服务器信息）
        "-H", "--header",
        # Basic Auth（允许提供凭证进行认证）
        "-u", "--user",
        # User-Agent
        "-A", "--user-agent",
        # 压缩
        "--compressed",
        # SSL
        "-k", "--insecure",
        "--cacert", "--capath",
        # 限速
        "--limit-rate",
        # 断点续传
        "-C", "--continue-at",
        # Cookie 只读
        "-b", "--cookie",
        # 重试
        "--retry", "--retry-delay",
    })
    # 封禁上传 / 发送请求体相关 flag
    blocked_flags: frozenset[str] = frozenset({
        "-F", "--form",
        "-d", "--data", "--data-ascii", "--data-binary", "--data-raw", "--data-urlencode",
        "-T", "--upload-file",
        "--ftp-ssl-ccc", "--ftp-pasv",
        "--mail-from", "--mail-rcpt",  # SMTP
        "--upload",
    })
    max_args: int = 30

    @classmethod
    def extract_paths(cls, args: list[str]) -> dict[str, list[str]]:
        # curl writes to the value of -o / --output. -O takes URL filename
        # in the *current directory*, so report cwd-relative '.'.
        writes = _collect_flag_value_targets(args, {"-o", "--output"})
        if any(a == "-O" or a == "--remote-name" for a in args):
            writes.append(".")
        return {"read": [], "write": writes}


@REGISTRY.register
class WgetCommand(SafeCommand):
    """wget — 只允许下载操作"""
    name = "wget"
    allowed_flags: frozenset[str] = frozenset({
        "-q", "--quiet",
        "-v", "--verbose",
        "-O", "--output-document",
        "-P", "--directory-prefix",
        "--timeout",
        "--tries",
        "--no-check-certificate",
        "--no-verbose",
        "-c", "--continue",
        "--limit-rate",
        "--wait",
        "--user-agent",
        "-r", "--recursive",
        "--level", "-l",
        "--no-parent",
        "--accept", "-A",
        "--reject", "-R",
        "--progress",
    })
    blocked_flags: frozenset[str] = frozenset({
        "--post-data", "--post-file",
        "--method",  # 自定义 HTTP 方法
        "--body-data", "--body-file",
    })
    max_args: int = 25

    @classmethod
    def extract_paths(cls, args: list[str]) -> dict[str, list[str]]:
        writes = _collect_flag_value_targets(
            args, {"-O", "--output-document", "-P", "--directory-prefix"}
        )
        # If no explicit output target, wget writes to current directory.
        if not writes:
            writes = ["."]
        return {"read": [], "write": writes}
