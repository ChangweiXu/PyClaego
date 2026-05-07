"""mcp_context — 支持 MCP 工具服务器的上下文处理器子模块

提供：
  BaseMCPContextHandler  抽象基类，继承 BaseContextHandlerV3，
                         内置 MCP Server 连接管理和工具调用拦截

  MCPClient              单个 MCP Server 连接管理器（stdio / SSE）

  StdioTransportConfig   本地子进程传输配置
  SSETransportConfig     远程 HTTP SSE 传输配置
  MCPServerConfig        两种传输配置的联合类型

  make_mcp_tool_name     工具名构造辅助函数
  parse_mcp_tool_name    工具名解析辅助函数

使用示例::

    from pyclaego.src.context.mcp_context import (
        BaseMCPContextHandler,
        SSETransportConfig,
        MCPServerConfig,
    )

    class KnowledgeBaseContextHandler(BaseMCPContextHandler):
        def get_mcp_server_configs(self) -> Dict[str, MCPServerConfig]:
            return {
                "kbase": SSETransportConfig(url="http://localhost:8080/mcp/sse"),
            }

        async def get_system_prompt(self) -> Optional[str]:
            return "你是一个知识库助手，可以通过 MCP 工具搜索和读取知识条目。"
"""

from .base_mcp_context import BaseMCPContextHandler
from .mcp_client import MCPClient
from .types import (
    MCP_TOOL_PREFIX,
    MCP_TOOL_SEP,
    MCPServerConfig,
    SSETransportConfig,
    StdioTransportConfig,
    make_mcp_tool_name,
    parse_mcp_tool_name,
)

# from .kbase_context import KbaseContextHandler

__all__ = [
    # 抽象基类
    "BaseMCPContextHandler",
    # 具体实现
    # "KbaseContextHandler",
    # MCPClient
    "MCPClient",
    # 传输配置
    "StdioTransportConfig",
    "SSETransportConfig",
    "MCPServerConfig",
    # 工具名辅助
    "make_mcp_tool_name",
    "parse_mcp_tool_name",
    "MCP_TOOL_PREFIX",
    "MCP_TOOL_SEP",
]
