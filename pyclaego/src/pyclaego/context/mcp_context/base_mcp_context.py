"""BaseMCPContextHandler — 支持 MCP 工具服务器的上下文处理器抽象基类

继承 BaseContextHandlerV3，在 SimpleContextHandlerV2 的 group_id 历史分组机制之上，
增加 MCP Server 连接管理和工具调用拦截能力。

生命周期扩展（相对于 BaseContextHandlerV3）：

  handle_before_loop
    → 第一次调用时懒连接所有 MCP Servers
    → 从历史文件加载并分组历史消息（与 SimpleContextHandlerV2 一致）
    → 合并本地工具 + MCP 工具，构建 tool_list

  handle_memory_tool_calls
    → 拦截 MCP 工具调用（通过前缀 ``mcp__{server_name}__`` 识别）
    → 直接通过 MCPClient 执行，结果作为 memory_tool_results 返回
    → 未拦截的工具调用作为 non_memory_calls 交给 Agent

  close()
    → 主动断开所有 MCP Server 连接（需在 Session 生命周期结束时调用）

子类必须实现：
  get_mcp_server_configs() → Dict[str, MCPServerConfig]
    返回 {server_name: config} 字典，定义要连接的 MCP Servers

  get_system_prompt()      → Optional[str]（async）
    返回 MCP 场景专属的系统提示词

子类可选覆盖：
  _include_local_tools()   → bool（默认 True）
    返回 False 时跳过 ToolManager 中的本地工具，仅使用 MCP 工具
"""

from __future__ import annotations

import traceback
import uuid
from abc import abstractmethod
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from ...llm import (
    ReasoningArtifact,
    ToolCall,
    ToolCallResult,
    ToolDefinition,
    UnifiedMessage,
    tool_description_to_definition,
)
from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2
from ..base_context import BaseContextHandlerV3
from .mcp_client import MCPClient
from .types import MCPServerConfig

_rlog = get_running_log()


class BaseMCPContextHandler(BaseContextHandlerV3):
    """支持 MCP 工具服务器的上下文处理器抽象基类

    与 SimpleContextHandlerV2 完全兼容的 group_id 历史分组机制，
    额外提供：
    - MCP Server 连接池管理（懒连接，显式 ``close()`` 释放）
    - ``handle_memory_tool_calls`` 中拦截并执行 MCP 工具调用
    - 合并本地工具 + MCP 工具的 tool_list 构建
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        """
        Args:
            session_id:           会话 ID
            workspace_path:       工作空间路径
            config:               上下文配置字典
            session_task_handler: 任务处理器（V2 版本）
        """
        super().__init__(session_id, workspace_path, config, session_task_handler)

        # 历史分组配置（与 SimpleContextHandlerV2 一致）
        strategy_config = self.config.get("mcp", {})
        self.keep_groups: int = strategy_config.get(
            "keep_groups",
            strategy_config.get("max_messages", 10) // 2,
        )
        self.max_messages: int = strategy_config.get(
            "max_messages", self.keep_groups * 2
        )

        # 本轮待写盘消息
        self._pending_messages: list[dict[str, Any]] = []
        # 当前对话组 ID
        self._current_group_id: str | None = None
        # 内存中的 UnifiedMessage 链
        self._messages: list[UnifiedMessage] = []

        # MCP 连接池
        self._mcp_clients: dict[str, MCPClient] = {}
        self._mcp_connected: bool = False

        _rlog.info(
            f"session_{session_id}",
            f"[{type(self).__name__}] 已初始化 "
            f"(keep_groups={self.keep_groups})",
        )

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    def get_mcp_server_configs(self) -> dict[str, MCPServerConfig]:
        """返回要连接的 MCP Server 配置字典

        Returns:
            ``{server_name: MCPServerConfig}`` 字典，
            server_name 作为工具名前缀的一部分（``mcp__{server_name}__``）

        示例::

            return {
                "kbase": SSETransportConfig(url="http://localhost:8080/mcp/sse"),
                "filesystem": StdioTransportConfig(
                    command="npx",
                    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                ),
            }
        """
        raise NotImplementedError("Subclass must implement get_mcp_server_configs()")

    @abstractmethod
    async def get_system_prompt(self) -> str | None:
        """返回系统提示词（async）

        子类应根据 MCP Server 提供的能力，定制专属的系统提示词。

        Returns:
            系统提示词字符串，或 None（不设置 system）
        """
        raise NotImplementedError("Subclass must implement get_system_prompt()")

    # ------------------------------------------------------------------
    # 子类可选覆盖
    # ------------------------------------------------------------------

    def _include_local_tools(self) -> bool:
        """是否将 ToolManager 中的本地工具纳入 tool_list

        Returns:
            True（默认）：本地工具 + MCP 工具均可用
            False：仅使用 MCP 工具（纯 MCP 场景）
        """
        return True

    # ------------------------------------------------------------------
    # MCP 连接管理
    # ------------------------------------------------------------------

    async def connect_mcp_servers(self) -> None:
        """连接所有 MCP Server（幂等，已连接则跳过）

        从 ``get_mcp_server_configs()`` 获取配置，逐个创建并连接 MCPClient。
        单个 Server 连接失败时记录错误日志并继续（不中断其他 Server 的连接）。
        也会检测已断开的客户端并尝试重连（lazy reconnect）。
        """
        if self._mcp_connected:
            # Check for stale connections and attempt lazy reconnect
            stale = [
                name for name, client in self._mcp_clients.items()
                if not client._connected
            ]
            if not stale:
                return
            # Remove stale clients so they get recreated below
            for name in stale:
                old = self._mcp_clients.pop(name)
                try:
                    await old.disconnect()
                except Exception:
                    pass
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[{type(self).__name__}] MCP Server 连接已失效，尝试重连: {name!r}",
                )
            self._mcp_connected = False

        configs = self.get_mcp_server_configs()
        if not configs:
            _rlog.info(
                f"session_{self.session_id}",
                f"[{type(self).__name__}] get_mcp_server_configs() 返回空，跳过 MCP 连接",
            )
            self._mcp_connected = True
            return

        for server_name, server_config in configs.items():
            client = MCPClient(server_name, server_config)
            try:
                await client.connect()
                self._mcp_clients[server_name] = client
                if self._session_task_handler:
                    await self._session_task_handler.log_info(
                        f"[{type(self).__name__}] MCP Server 已连接: {server_name!r} "
                        f"({len(await client.list_tools())} 个工具)"
                    )
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[{type(self).__name__}] MCP Server 连接失败，已跳过: "
                    f"{server_name!r} - {e}\n{traceback.format_exc()}",
                )

        self._mcp_connected = True

    async def close(self) -> None:
        """断开所有 MCP Server 连接，释放资源

        应在 Session 生命周期结束时调用（例如在 Agent 的 finally 块中）。
        多次调用安全（幂等）。
        """
        for server_name, client in self._mcp_clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[{type(self).__name__}] 断开 MCP Server 时发生异常: "
                    f"{server_name!r} - {e}",
                )
        self._mcp_clients.clear()
        self._mcp_connected = False

    # ------------------------------------------------------------------
    # BaseContextHandlerV3 生命周期方法实现
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: dict[str, Any]) -> dict[str, Any]:
        """每轮对话开始前：懒连接 MCP Servers，构建历史上下文 + 工具列表

        Args:
            user_msg: 用户消息 dict（``role``, ``content``, ``timestamp`` 等）

        Returns:
            LLM 上下文字典::

                {
                    "system":    Optional[str],
                    "messages":  List[UnifiedMessage],
                    "tool_list": Optional[List[ToolDefinition]],
                }
        """
        if not self._session_task_handler:
            raise ValueError(f"[{type(self).__name__}] 未传入 SessionTaskHandlerV2")

        # 1. 懒连接 MCP Servers（首次调用时执行）
        if not self._mcp_connected:
            await self.connect_mcp_servers()

        # 2. 为新 user 消息分配 group_id
        if "group_id" not in user_msg:
            self._current_group_id = self._generate_group_id()
            user_msg["group_id"] = self._current_group_id
            await self._session_task_handler.log_info(
                f"[{type(self).__name__}] 为 user 消息分配 group_id: {self._current_group_id}"
            )

        # 3. 暂存 user 消息
        self._pending_messages.append(user_msg)

        # 4. 构建上下文
        system: str | None = await self.get_system_prompt()
        self._messages = await self._build_unified_messages()
        tool_list: list[ToolDefinition] | None = await self._build_tool_list()

        # 5. 将当前 user 消息追加到 _messages
        raw_parts = user_msg.get("content_parts")
        if raw_parts:
            def _deser(d: dict):
                return BaseContextHandlerV3.deserialize_content_part(d)

            parts = [_deser(p) for p in raw_parts]
            user_unified = UnifiedMessage(role="user", content_parts=parts)
        else:
            user_unified = UnifiedMessage(role="user", text=user_msg.get("content", ""))
        self._messages.append(user_unified)

        await self._session_task_handler.log_info(
            f"[{type(self).__name__}] 上下文构建完成: "
            f"{len(self._messages)} 条消息, "
            f"{len(tool_list) if tool_list else 0} 个工具"
        )

        return {
            "system": system,
            "messages": self._messages,
            "tool_list": tool_list,
        }

    async def handle_after_llm_call(
        self,
        text_reply: str = "",
        tool_calls: list[ToolCall] | None = None,
        reasoning: ReasoningArtifact | None = None,
        produced_by_provider: str | None = None,
        produced_by_model: str | None = None,
    ) -> list[UnifiedMessage]:
        """LLM 调用后处理：暂存 assistant 消息

        Args:
            text_reply: LLM 文本回复
            tool_calls: 工具调用列表（如有）
            reasoning: provider 思考产物（多态 ReasoningArtifact，必须原样回传）
            produced_by_provider: 生成此消息的 provider 标签
            produced_by_model: 生成此消息的具体模型名

        Returns:
            更新后的 UnifiedMessage 列表（包含本次 assistant 消息）
        """
        if not self._session_task_handler:
            raise ValueError(f"[{type(self).__name__}] 未传入 SessionTaskHandlerV2")

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text_reply or "",
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
        }
        if self._current_group_id:
            assistant_msg["group_id"] = self._current_group_id
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ]

        # 思考模式产出（必须同时写盘 + 下轮请求原样回传）
        if reasoning:
            assistant_msg["reasoning"] = reasoning.to_dict()
        # provider/model 标签
        if produced_by_provider:
            assistant_msg["produced_by_provider"] = produced_by_provider
        if produced_by_model:
            assistant_msg["produced_by_model"] = produced_by_model

        self._pending_messages.append(assistant_msg)
        self._messages.append(
            UnifiedMessage(
                role="assistant",
                text=text_reply,
                tool_calls=tool_calls,
                reasoning=reasoning,
                produced_by_provider=produced_by_provider,
                produced_by_model=produced_by_model,
            )
        )

        await self._session_task_handler.log_info(
            f"[{type(self).__name__}] 暂存 assistant 消息 "
            f"(tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"messages={len(self._messages)}, pending={len(self._pending_messages)})"
        )
        return self._messages

    async def handle_memory_tool_calls(
        self,
        tool_calls: list[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any] | None:
        """拦截 MCP 工具调用，直接通过 MCPClient 执行

        将所有工具调用分为两类：
        - MCP 工具（前缀为 ``mcp__{server_name}__``）：在此处执行，结果作为 memory_tool_results 返回
        - 本地工具（无 MCP 前缀）：作为 non_memory_calls 交给 Agent 通过 SecurityHandler 执行

        Args:
            tool_calls:       本轮所有工具调用（来自 LLM 响应）
            loop_task_handler: 当前循环的任务处理器（用于日志）

        Returns:
            ``{"memory_tool_results": [...], "non_memory_calls": [...]}``
        """
        mcp_calls = [tc for tc in tool_calls if self._is_mcp_tool(tc.name)]
        non_mcp_calls = [tc for tc in tool_calls if not self._is_mcp_tool(tc.name)]

        if not mcp_calls:
            await loop_task_handler.log_info(
                f"[{type(self).__name__}] 无 MCP 工具调用，全部 {len(tool_calls)} 个作为普通工具"
            )
            return {"memory_tool_results": [], "non_memory_calls": tool_calls}

        await loop_task_handler.log_info(
            f"[{type(self).__name__}] 拦截 {len(mcp_calls)} 个 MCP 工具调用，"
            f"{len(non_mcp_calls)} 个普通工具继续执行"
        )

        results: list[ToolCallResult] = []
        for tc in mcp_calls:
            result = await self._execute_mcp_tool(tc, loop_task_handler)
            results.append(result)

        return {"memory_tool_results": results, "non_memory_calls": non_mcp_calls}

    async def handle_after_tool_calls(
        self,
        tool_results: list[ToolCallResult],
        last_call_prompt: str | None = None,
    ) -> list[UnifiedMessage]:
        """工具调用后处理：暂存工具结果消息

        Args:
            tool_results: 所有工具执行结果（MCP + 本地工具合并后）
            last_call_prompt: 可选。最后一轮时传入，附加到消息 content，提示 LLM 直接作答。

        Returns:
            更新后的 UnifiedMessage 列表
        """
        if not self._session_task_handler:
            raise ValueError(f"[{type(self).__name__}] 未传入 SessionTaskHandlerV2")

        if not tool_results:
            return self._messages

        user_msg: dict[str, Any] = {
            "role": "user",
            "tool_results": [
                {
                    "tool_call_id": tr.tool_call_id,
                    "tool_name": tr.tool_name,
                    "content": tr.content,
                }
                for tr in tool_results
            ],
            "timestamp": datetime.now().isoformat(),
            "type": "tool_result",
        }
        if last_call_prompt:
            user_msg["content"] = last_call_prompt
        if self._current_group_id:
            user_msg["group_id"] = self._current_group_id

        self._pending_messages.append(user_msg)
        self._messages.append(UnifiedMessage(
            role="user",
            tool_results=tool_results,
            text=last_call_prompt or None,
        ))

        await self._session_task_handler.log_info(
            f"[{type(self).__name__}] 暂存工具结果 "
            f"(results={len(tool_results)}, messages={len(self._messages)}, "
            f"pending={len(self._pending_messages)}, last_call={bool(last_call_prompt)})"
        )
        return self._messages

    async def handle_after_loop(self, final_message: dict[str, Any]) -> None:
        """对话循环结束：批量写盘，清空暂存状态

        Args:
            final_message: 最终 assistant 消息 dict
        """
        if not self._session_task_handler:
            raise ValueError(f"[{type(self).__name__}] 未传入 SessionTaskHandlerV2")

        # 附加 group_id
        if self._current_group_id and "group_id" not in final_message:
            final_message["group_id"] = self._current_group_id

        # 去重：若 pending 最后一条已是同内容的 assistant 消息，则跳过
        if final_message.get("content"):
            should_append = True
            if self._pending_messages:
                last = self._pending_messages[-1]
                if (
                    last.get("role") == "assistant"
                    and last.get("content") == final_message.get("content")
                ):
                    should_append = False
            if should_append:
                self._pending_messages.append(final_message)

        # 批量写盘
        if self._pending_messages:
            ok = self.history_manager.append_messages(self._pending_messages)
            await self._session_task_handler.log_info(
                f"[{type(self).__name__}] 批量写盘 "
                f"{len(self._pending_messages)} 条消息 (ok={ok})"
            )
            self._pending_messages.clear()
            self._current_group_id = None
            self._messages.clear()

    async def handle_interruption(self) -> int:
        """中断处理：丢弃未写盘消息，断开 MCP 连接"""
        discarded = await super().handle_interruption()
        await self.close()
        return discarded

    # ------------------------------------------------------------------
    # 工具路由辅助
    # ------------------------------------------------------------------

    def _is_mcp_tool(self, name: str) -> bool:
        """判断工具名是否为任一 MCP Server 的工具"""
        return any(client.is_my_tool(name) for client in self._mcp_clients.values())

    async def _execute_mcp_tool(
        self,
        tool_call: ToolCall,
        task_handler: SessionTaskHandlerV2 | None = None,
    ) -> ToolCallResult:
        """通过 MCPClient 执行一次 MCP 工具调用

        单个调用失败时返回包含错误信息的 ToolCallResult，而非抛出异常。

        Args:
            tool_call:    要执行的工具调用
            task_handler: 日志处理器（可选）

        Returns:
            ToolCallResult
        """
        for client in self._mcp_clients.values():
            if client.is_my_tool(tool_call.name):
                try:
                    result = await client.call_tool(
                        prefixed_name=tool_call.name,
                        arguments=tool_call.arguments,
                        tool_call_id=tool_call.id,
                    )
                    if task_handler:
                        await task_handler.log_info(
                            f"[{type(self).__name__}] MCP 工具调用成功: {tool_call.name!r}"
                        )
                    return result
                except Exception as e:
                    err_msg = f"MCP 工具调用异常: {tool_call.name!r}: {e}"
                    _rlog.error(
                        f"session_{self.session_id}",
                        f"[{type(self).__name__}] {err_msg}\n{traceback.format_exc()}",
                    )
                    return ToolCallResult(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        content=f"[Error] {err_msg}",
                    )

        # 不应出现的情况（调用前已通过 _is_mcp_tool 过滤）
        return ToolCallResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            content=f"[Error] 找不到处理工具 {tool_call.name!r} 的 MCP Client",
        )

    # ------------------------------------------------------------------
    # 工具列表构建
    # ------------------------------------------------------------------

    async def _build_tool_list(self) -> list[ToolDefinition] | None:
        """构建 tool_list：本地工具（可选）+ 所有已连接 MCP Server 的工具

        Returns:
            合并后的 ToolDefinition 列表；若为空则返回 None
        """
        tool_defs: list[ToolDefinition] = []

        # 本地工具（来自 ToolManager）
        if self._include_local_tools():
            try:
                from ...tool import get_tool_manager
                tool_manager = get_tool_manager()
                for tool_name in tool_manager.list_loaded_tools():
                    tool = tool_manager.get_tool(tool_name)
                    if tool and tool.is_enabled():
                        tool_defs.append(tool_description_to_definition(tool.get_description()))
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[{type(self).__name__}] 加载本地工具失败: {e}",
                )

        # MCP 工具
        for server_name, client in self._mcp_clients.items():
            if not client.connected:
                continue
            try:
                mcp_tools = await client.list_tools()
                tool_defs.extend(mcp_tools)
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[{type(self).__name__}] 获取 MCP 工具列表失败: "
                    f"server={server_name!r} - {e}",
                )

        return tool_defs if tool_defs else None

    # ------------------------------------------------------------------
    # 历史消息管理（与 SimpleContextHandlerV2 保持一致）
    # ------------------------------------------------------------------

    async def get_recent_messages(self, count: int) -> list[dict[str, Any]]:
        """获取最近 count 条消息（保证第一条为 user 消息）"""
        if count <= 0:
            return []
        msgs = self.history_manager.load_all()
        grouped, dirty = await self._group_messages_by_id(msgs)
        await self._writeback_if_dirty(msgs, dirty)
        keep_groups = max(1, (count + 1) // 2)
        return await self._take_recent_groups(grouped, keep_groups)

    async def _build_unified_messages(self) -> list[UnifiedMessage]:
        """从历史文件加载并转换为 UnifiedMessage 列表"""
        return self.records_to_unified_messages(await self._get_recent_messages())

    async def _get_recent_messages(self) -> list[dict[str, Any]]:
        """按 group_id 分组后取最近 keep_groups 组"""
        msgs = self.history_manager.load_all()
        grouped, dirty = await self._group_messages_by_id(msgs)
        await self._writeback_if_dirty(msgs, dirty)
        return await self._take_recent_groups(grouped, self.keep_groups)

    async def _writeback_if_dirty(
        self, msgs: list[dict[str, Any]], dirty: bool
    ) -> None:
        """若有消息被回填了 group_id，写回历史文件（一次性迁移）"""
        if not dirty:
            return
        ok = self.history_manager.save_all(msgs)
        msg = f"[{type(self).__name__}] 已将推断的 group_id 回写历史文件 (ok={ok})"
        if self._session_task_handler:
            await self._session_task_handler.log_info(msg)
        else:
            _rlog.info(f"session_{self.session_id}", msg)

    def _generate_group_id(self) -> str:
        """生成唯一的对话组 ID（格式: ``g_{timestamp}_{6位UUID}``）"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"g_{timestamp}_{uuid.uuid4().hex[:6]}"

    async def _group_messages_by_id(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[list[dict[str, Any]]], bool]:
        """将消息列表按 group_id 分组

        对缺失 group_id 的旧格式消息，按角色转换推断分组边界（in-place 回填）。

        Returns:
            ``(groups, dirty)``：groups 为按组分组的列表，dirty 标记是否需要回写
        """
        groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        last_group_id: str | None = None
        dirty: bool = False

        for msg in messages:
            group_id = msg.get("group_id")

            if group_id is not None:
                last_group_id = group_id
            else:
                is_new_turn = (
                    msg.get("role") == "user"
                    and msg.get("type") != "tool_result"
                )
                if is_new_turn or last_group_id is None:
                    last_group_id = f"g_legacy_{uuid.uuid4().hex[:6]}"
                    _rlog.warning(
                        f"session_{self.session_id}",
                        f"[{type(self).__name__}] 消息缺失 group_id，推断新组: {last_group_id}",
                    )
                group_id = last_group_id
                msg["group_id"] = group_id
                dirty = True

            if group_id not in groups:
                groups[group_id] = []
            groups[group_id].append(msg)

        return list(groups.values()), dirty

    async def _take_recent_groups(
        self,
        grouped_messages: list[list[dict[str, Any]]],
        keep_groups: int,
    ) -> list[dict[str, Any]]:
        """保留最近 N 组，扁平化后确保第一条为 user 消息"""
        if not grouped_messages:
            return []

        recent = (
            grouped_messages[-keep_groups:]
            if len(grouped_messages) > keep_groups
            else grouped_messages
        )

        flattened: list[dict[str, Any]] = []
        for group in recent:
            flattened.extend(group)

        # 边界保护：确保第一条为 user 消息
        while flattened and flattened[0].get("role") != "user":
            _rlog.warning(
                f"session_{self.session_id}",
                f"[{type(self).__name__}] 截取后首条非 user 消息，丢弃: "
                f"role={flattened[0].get('role')!r}",
            )
            flattened.pop(0)

        return flattened

    # ------------------------------------------------------------------
    # 信息查询
    # ------------------------------------------------------------------

    def get_info(self) -> dict[str, Any]:
        """获取上下文处理器状态信息"""
        info = super().get_info()
        info.update(
            {
                "keep_groups": self.keep_groups,
                "max_messages": self.max_messages,
                "pending_messages": len(self._pending_messages),
                "current_group_id": self._current_group_id,
                "mcp_connected": self._mcp_connected,
                "mcp_servers": {
                    name: {
                        "connected": client.connected,
                        "tool_count": len(client._tool_defs_cache),
                    }
                    for name, client in self._mcp_clients.items()
                },
                "history_manager": self.history_manager.get_info(),
            }
        )
        return info
