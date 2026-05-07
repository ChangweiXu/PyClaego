"""MCPClient — 单个 MCP Server 连接管理器

封装与一个 MCP Server 的完整生命周期：连接、工具发现、工具调用、断开。
支持两种传输方式（由 MCPServerConfig 决定）：
  - StdioTransportConfig : 本地子进程（stdio_client）
  - SSETransportConfig   : 远程 HTTP SSE（sse_client）

使用方式::

    client = MCPClient("kbase", SSETransportConfig(url="http://localhost:8080/mcp/sse"))
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("mcp__kbase__search", {"query": "..."}, tool_call_id="tc_1")
    await client.disconnect()

工具命名约定：工具名以 ``mcp__{server_name}__`` 为前缀，与 ToolManager 工具名不冲突。
"""

from __future__ import annotations

from contextlib import AsyncExitStack

from ...llm import ToolCallResult, ToolDefinition
from ...llm.types import ImagePart
from ...logging import get_running_log
from .types import (
    MCP_TOOL_PREFIX,
    MCP_TOOL_SEP,
    MCPServerConfig,
    SSETransportConfig,
    StdioTransportConfig,
)

_rlog = get_running_log()

# 延迟导入 mcp SDK，避免在未安装 SDK 时影响模块加载
# 实际导入在 connect() 中完成


class MCPClient:
    """单个 MCP Server 连接管理器

    每个实例对应一个 MCP Server，通过 ``connect()`` 建立连接，
    通过 ``disconnect()`` 释放资源。连接期间缓存工具列表以避免重复 RPC。

    Attributes:
        server_name: MCP Server 的唯一标识名（由调用方自定义，用于工具名前缀）
    """

    def __init__(self, server_name: str, config: MCPServerConfig) -> None:
        """
        Args:
            server_name: MCP Server 唯一标识名（例如 "kbase"、"filesystem"）
            config:      传输配置（StdioTransportConfig 或 SSETransportConfig）
        """
        self.server_name: str = server_name
        self._config: MCPServerConfig = config
        self._exit_stack: AsyncExitStack = AsyncExitStack()
        self._session = None  # mcp.ClientSession，类型标注延迟到运行时
        self._tool_defs_cache: list[ToolDefinition] = []
        self._connected: bool = False

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """连接到 MCP Server

        根据传输配置类型选择 stdio_client 或 sse_client，
        建立 ClientSession，发送 initialize 握手，并缓存工具列表。

        Raises:
            ImportError:   未安装 ``mcp`` SDK
            RuntimeError:  连接或初始化失败
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ImportError(
                "MCPClient 需要安装 mcp SDK：pip install mcp"
            ) from exc

        try:
            if isinstance(self._config, StdioTransportConfig):
                server_params = StdioServerParameters(
                    command=self._config.command,
                    args=self._config.args,
                    env=self._config.env or None,
                )
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
            elif isinstance(self._config, SSETransportConfig):
                read, write = await self._exit_stack.enter_async_context(
                    sse_client(
                        url=self._config.url,
                        headers=self._config.headers or None,
                        sse_read_timeout=60,
                    )
                )
            else:
                raise ValueError(f"未知的 MCPServerConfig 类型: {type(self._config)}")

            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
            await self.refresh_tools()
            self._connected = True

            _rlog.info(
                "mcp_client",
                f"[MCPClient] 已连接: server={self.server_name!r}, "
                f"tools={len(self._tool_defs_cache)}",
            )

        except Exception:
            await self._exit_stack.aclose()
            self._session = None
            raise

    async def disconnect(self) -> None:
        """断开连接，释放所有资源（幂等，多次调用安全）"""
        try:
            await self._exit_stack.aclose()
        except Exception as e:
            _rlog.warning(
                "mcp_client",
                f"[MCPClient] 断开连接时发生异常: server={self.server_name!r}: {e}",
            )
        finally:
            self._session = None
            self._connected = False
            self._tool_defs_cache.clear()
            _rlog.info(
                "mcp_client",
                f"[MCPClient] 已断开连接: server={self.server_name!r}",
            )

    # ------------------------------------------------------------------
    # 工具查询
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[ToolDefinition]:
        """返回缓存的工具列表（带 ``mcp__{server_name}__`` 前缀）

        Returns:
            ToolDefinition 列表（浅拷贝）
        """
        return list(self._tool_defs_cache)

    async def refresh_tools(self) -> None:
        """从 MCP Server 重新获取工具列表，更新缓存

        Raises:
            RuntimeError: 未连接时调用
        """
        if not self._session:
            raise RuntimeError(f"MCPClient({self.server_name!r}) 尚未连接，无法刷新工具列表")
        result = await self._session.list_tools()
        self._tool_defs_cache = [
            self._convert_tool_definition(tool) for tool in result.tools
        ]

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        prefixed_name: str,
        arguments: dict,
        tool_call_id: str,
    ) -> ToolCallResult:
        """调用 MCP 工具

        Args:
            prefixed_name: 带前缀的工具名，例如 ``mcp__kbase__search``
            arguments:     工具参数 dict（与 ToolCall.arguments 一致）
            tool_call_id:  对应的 ToolCall.id（原样回传给 ToolCallResult）

        Returns:
            ToolCallResult（文本内容 + 可选多模态内容）

        Raises:
            RuntimeError: 未连接时调用
        """
        if not self._session:
            raise RuntimeError(f"MCPClient({self.server_name!r}) 尚未连接")

        real_name = self._strip_prefix(prefixed_name)
        result = await self._session.call_tool(real_name, arguments)
        return self._convert_tool_result(result, tool_call_id, prefixed_name)

    # ------------------------------------------------------------------
    # 工具名路由辅助
    # ------------------------------------------------------------------

    def is_my_tool(self, name: str) -> bool:
        """判断带前缀的工具名是否属于本 Server

        Args:
            name: 待检查的工具名

        Returns:
            True 当且仅当 name 以 ``mcp__{server_name}__`` 开头
        """
        return name.startswith(self._tool_prefix())

    @property
    def connected(self) -> bool:
        """是否已建立连接"""
        return self._connected

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _tool_prefix(self) -> str:
        """返回本 Server 的工具名前缀（含尾部分隔符）"""
        return f"{MCP_TOOL_PREFIX}{MCP_TOOL_SEP}{self.server_name}{MCP_TOOL_SEP}"

    def _make_prefixed_name(self, tool_name: str) -> str:
        """为 MCP Server 原始工具名添加前缀"""
        return f"{self._tool_prefix()}{tool_name}"

    def _strip_prefix(self, prefixed_name: str) -> str:
        """去除工具名前缀，还原 MCP Server 原始工具名"""
        prefix = self._tool_prefix()
        if prefixed_name.startswith(prefix):
            return prefixed_name[len(prefix):]
        return prefixed_name

    def _convert_tool_definition(self, mcp_tool: object) -> ToolDefinition:
        """将 ``mcp.types.Tool`` 转换为 ``ToolDefinition``

        Args:
            mcp_tool: ``mcp.types.Tool`` 实例

        Returns:
            ToolDefinition（工具名已加前缀）
        """
        name: str = getattr(mcp_tool, "name", "")
        description: str = getattr(mcp_tool, "description", "") or ""
        input_schema: dict = getattr(mcp_tool, "inputSchema", {}) or {}

        return ToolDefinition(
            name=self._make_prefixed_name(name),
            description=description,
            parameters=dict(input_schema),
        )

    def _convert_tool_result(
        self,
        result: object,
        tool_call_id: str,
        tool_name: str,
    ) -> ToolCallResult:
        """将 ``mcp.types.CallToolResult`` 转换为 ``ToolCallResult``

        支持的内容块类型：
        - ``TextContent``     → 追加到 content 字符串
        - ``ImageContent``    → 转换为 ImagePart（base64）
        - 其他（含 isError）  → 尝试提取 text 属性作为文本回退

        Args:
            result:       ``mcp.types.CallToolResult`` 实例
            tool_call_id: 原始 ToolCall.id
            tool_name:    原始 prefixed_name（用于 ToolCallResult.tool_name）

        Returns:
            ToolCallResult
        """
        text_parts: list[str] = []
        multimodal_parts = []

        is_error: bool = getattr(result, "isError", False)
        content_blocks = getattr(result, "content", []) or []

        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", "") or ""
                text_parts.append(text)
            elif block_type == "image":
                data = getattr(block, "data", "") or ""
                mime_type = getattr(block, "mimeType", "image/png") or "image/png"
                multimodal_parts.append(
                    ImagePart(source_type="base64", data=data, media_type=mime_type)
                )
            else:
                # EmbeddedResource 或未知类型：尝试提取 text 属性
                fallback = getattr(block, "text", None)
                if fallback is not None:
                    text_parts.append(str(fallback))

        combined_text = "\n".join(text_parts)
        if is_error and combined_text:
            combined_text = f"[MCP Error] {combined_text}"

        return ToolCallResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content=combined_text,
            content_parts=multimodal_parts if multimodal_parts else None,
        )

    def __repr__(self) -> str:
        return (
            f"MCPClient("
            f"server_name={self.server_name!r}, "
            f"connected={self._connected}, "
            f"tools={len(self._tool_defs_cache)})"
        )
