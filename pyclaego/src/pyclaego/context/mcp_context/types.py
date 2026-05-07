"""MCP Context 传输配置类型定义

定义连接 MCP Server 所需的两种传输方式配置：
  - StdioTransportConfig : 本地子进程（stdin/stdout），适用于 npx/uvx 托管的 MCP Server
  - SSETransportConfig   : 远程 HTTP SSE，适用于已部署的 MCP SSE 端点

工具命名约定：
  MCP 工具在 ToolDefinition 中以前缀区分来源：
    mcp__{server_name}__{tool_name}
  例：
    mcp__filesystem__read_file
    mcp__kbase__search
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

# ─────────────────────────────────────────────────────────────────
# 工具命名约定常量
# ─────────────────────────────────────────────────────────────────

MCP_TOOL_PREFIX: str = "mcp"
"""所有 MCP 工具名的顶层前缀"""

MCP_TOOL_SEP: str = "__"
"""前缀各部分之间的分隔符"""


def make_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """构造带前缀的 MCP 工具名

    Args:
        server_name: MCP Server 名称（自定义，唯一标识符）
        tool_name:   MCP Server 原始工具名

    Returns:
        带前缀的工具名，格式: ``mcp__{server_name}__{tool_name}``
    """
    return f"{MCP_TOOL_PREFIX}{MCP_TOOL_SEP}{server_name}{MCP_TOOL_SEP}{tool_name}"


def parse_mcp_tool_name(prefixed_name: str) -> tuple[str, str] | None:
    """解析带前缀的 MCP 工具名

    Args:
        prefixed_name: 带前缀的工具名，例如 ``mcp__kbase__search``

    Returns:
        ``(server_name, tool_name)`` 元组；若格式不符则返回 None
    """
    if not prefixed_name.startswith(f"{MCP_TOOL_PREFIX}{MCP_TOOL_SEP}"):
        return None
    rest = prefixed_name[len(MCP_TOOL_PREFIX) + len(MCP_TOOL_SEP):]
    sep_idx = rest.find(MCP_TOOL_SEP)
    if sep_idx == -1:
        return None
    return rest[:sep_idx], rest[sep_idx + len(MCP_TOOL_SEP):]


# ─────────────────────────────────────────────────────────────────
# 传输配置
# ─────────────────────────────────────────────────────────────────

@dataclass
class StdioTransportConfig:
    """本地子进程（stdio）传输配置

    通过 stdin/stdout 与本地 MCP Server 子进程通信，适用于
    ``npx @modelcontextprotocol/server-filesystem`` 等本地工具。

    Attributes:
        command: 可执行程序路径（例如 "npx" 或 "uvx"）
        args:    命令行参数列表（例如 ["@modelcontextprotocol/server-filesystem", "/tmp"]）
        env:     附加到子进程的环境变量（空 dict 表示不添加额外变量）
    """

    command: str
    """可执行程序路径"""

    args: list[str] = field(default_factory=list)
    """命令行参数列表"""

    env: dict[str, str] = field(default_factory=dict)
    """附加到子进程的环境变量"""


@dataclass
class SSETransportConfig:
    """远程 HTTP SSE 传输配置

    通过 Server-Sent Events 协议连接远程 MCP 端点，适用于
    已独立部署的 MCP SSE 服务（例如 md_kbase 的 ``/mcp/sse``）。

    Attributes:
        url:     SSE 端点 URL（例如 "http://localhost:8080/mcp/sse"）
        headers: 附加请求头（例如 {"Authorization": "Bearer <token>"}）
    """

    url: str
    """SSE 端点 URL"""

    headers: dict[str, str] = field(default_factory=dict)
    """附加请求头"""


# ─────────────────────────────────────────────────────────────────
# 联合类型
# ─────────────────────────────────────────────────────────────────

MCPServerConfig = Union[StdioTransportConfig, SSETransportConfig]
"""MCP Server 传输配置联合类型（stdio 或 SSE）"""
