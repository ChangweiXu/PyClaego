"""InfoGathererContextHandler — 信息收集子 Agent 专属上下文处理器

在 BaseSubAgentContextHandler 基础上：
- 使用信息收集专属系统提示词
- 仅暴露信息读写相关工具集（排除 bash、read_image_base64 等）
- 自动截断过长的工具输出，防止消息列表膨胀
- 周期性压缩旧轮次的工具输出
"""

from pathlib import Path
from typing import Any

from ...llm import ToolCallResult, UnifiedMessage
from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2
from ..system_prompts.subagent_info_gatherer import INFO_GATHERER_SYSTEM_PROMPT
from .subagent_summarizing_context import SubAgentSummarizingContextHandler

_rlog = get_running_log()


class InfoGathererContextHandler(SubAgentSummarizingContextHandler):
    """信息收集子 Agent 专属上下文处理器

    与 BaseSubAgentContextHandler 的区别：
    - 使用 InfoGatherer 专属系统提示词
    - _build_tool_list() 仅返回 ALLOWED_TOOLS 中的工具
    - handle_after_tool_calls() 自动截断过长工具输出，并周期性压缩旧轮次
    """

    ALLOWED_TOOLS = frozenset({
        "download_file",
        "file_edit",
        "file_info",
        "file_line",
        "glob",
        "list_directory",
        "mkdir",
        "read_file",
        "read_image_base64",
        "search_text",
        "web_fetch",
        "web_search",
        "write_file",
    })

    # 默认压缩配置
    _DEFAULT_COMPRESS_CONFIG = {
        "tool_output_max_tokens": 3000,     # 单次工具输出最大 token 数
        "tool_output_keep_head": 2000,      # 截断时保留开头 token 数
        "tool_output_keep_tail": 500,       # 截断时保留结尾 token 数
        "context_token_budget": 50000,      # 消息列表总 token 预算
        "keep_recent_rounds": 3,            # 压缩时豁免最近 N 轮
    }

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        memory_mode: str = "empty",
        initial_messages: list | None = None,
        initial_system: str | None = None,
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        # 如果调用方未传入自定义系统提示词，使用 InfoGatherer 专属提示词
        if initial_system is None:
            initial_system = INFO_GATHERER_SYSTEM_PROMPT.format(
                workspace_path=str(workspace_path)
            )

        super().__init__(
            session_id=session_id,
            workspace_path=workspace_path,
            config=config,
            memory_mode=memory_mode,
            initial_messages=initial_messages,
            initial_system=initial_system,
            session_task_handler=session_task_handler,
        )

        # 压缩配置（从 context 切片读取，缺省使用默认值）
        strategy_cfg = self.config.get("info_gatherer", {})
        compress_cfg = strategy_cfg.get("compress", {})
        self._tool_output_max_tokens: int = int(
            compress_cfg.get(
                "tool_output_max_tokens",
                self._DEFAULT_COMPRESS_CONFIG["tool_output_max_tokens"],
            )
        )
        self._tool_output_keep_head: int = int(
            compress_cfg.get(
                "tool_output_keep_head",
                self._DEFAULT_COMPRESS_CONFIG["tool_output_keep_head"],
            )
        )
        self._tool_output_keep_tail: int = int(
            compress_cfg.get(
                "tool_output_keep_tail",
                self._DEFAULT_COMPRESS_CONFIG["tool_output_keep_tail"],
            )
        )
        self._context_token_budget: int = int(
            compress_cfg.get(
                "context_token_budget",
                self._DEFAULT_COMPRESS_CONFIG["context_token_budget"],
            )
        )
        self._keep_recent_rounds: int = int(
            compress_cfg.get(
                "keep_recent_rounds",
                self._DEFAULT_COMPRESS_CONFIG["keep_recent_rounds"],
            )
        )

        _rlog.info(
            f"session_{session_id}",
            f"[InfoGathererContextHandler] 初始化完成 "
            f"(allowed_tools={sorted(self.ALLOWED_TOOLS)}, workspace={workspace_path}, "
            f"tool_output_max_tokens={self._tool_output_max_tokens}, "
            f"tool_output_keep_head={self._tool_output_keep_head}, "
            f"tool_output_keep_tail={self._tool_output_keep_tail}, "
            f"context_token_budget={self._context_token_budget}, "
            f"keep_recent_rounds={self._keep_recent_rounds})",
        )

    # ------------------------------------------------------------------
    # Layer 1: 单次工具输出截断
    # ------------------------------------------------------------------

    def _truncate_tool_content(self, content: str, tool_name: str) -> str:
        """截断过长的工具输出，保留头部和尾部。

        Args:
            content: 原始工具输出
            tool_name: 工具名称（用于日志和截断标记）

        Returns:
            截断后的内容（若未超限则原样返回）
        """
        if not content:
            return content

        token_count = self._token_counter.count_tokens(content)
        if token_count <= self._tool_output_max_tokens:
            return content

        head = self._token_counter.truncate_text_to_tokens(
            content, self._tool_output_keep_head
        )
        tail = self._token_counter.truncate_text_to_tokens(
            content[-len(content) // 2:],  # 从后半段取尾部，避免全文编码
            self._tool_output_keep_tail,
        )
        # 如果 tail 提取异常（字符串太短），回退到直接从末尾截取
        if not tail:
            tail_chars = self._tool_output_keep_tail * 4  # 粗估
            tail = content[-tail_chars:] if len(content) > tail_chars else ""

        truncated_count = token_count - self._tool_output_keep_head - self._tool_output_keep_tail
        marker = (
            f"\n\n[... 已截断 ~{truncated_count} tokens "
            f"(原始 ~{token_count} tokens, 工具: {tool_name}) ...]\n\n"
        )

        _rlog.info(
            f"session_{self.session_id}",
            f"[InfoGathererCtx] 截断工具输出: {tool_name}, "
            f"{token_count} → ~{self._tool_output_keep_head + self._tool_output_keep_tail} tokens",
        )

        return head + marker + tail

    def _truncate_tool_results(
        self, tool_results: list[ToolCallResult]
    ) -> list[ToolCallResult]:
        """对一批工具结果逐个应用截断。"""
        return [
            ToolCallResult(
                tool_call_id=tr.tool_call_id,
                tool_name=tr.tool_name,
                content=self._truncate_tool_content(tr.content, tr.tool_name),
            )
            for tr in tool_results
        ]

    # ------------------------------------------------------------------
    # Layer 2: 旧轮次工具输出压缩
    # ------------------------------------------------------------------

    def _compress_old_rounds(self) -> None:
        """检查总 token 量，超预算时将旧轮次的工具输出替换为摘要桩。

        以 (assistant, user/tool_result) 为一轮，保留最近 keep_recent_rounds 轮
        不被压缩。更早的轮次中，tool_result 消息的内容被替换为简短桩。
        """
        # 快速估算：用 pending_messages（dict 列表）计算 token
        total_tokens = self._token_counter.count_messages_tokens(self._pending_messages)
        if total_tokens < self._context_token_budget:
            return

        # 找出所有 tool_result 消息的索引（在 _messages 和 _pending_messages 中同步）
        # _messages 结构: [user, assistant, tool_result, assistant, tool_result, ...]
        # 一轮 = 一个 assistant + 一个 tool_result
        tool_result_indices = [
            i for i, msg in enumerate(self._messages)
            if msg.tool_results is not None
        ]

        if not tool_result_indices:
            return

        # 确定可压缩边界：保留最近 keep_recent_rounds 个 tool_result 不动
        if len(tool_result_indices) <= self._keep_recent_rounds:
            return
        compressible = tool_result_indices[:-self._keep_recent_rounds]

        compressed_count = 0
        for msg_idx in compressible:
            msg = self._messages[msg_idx]
            if msg.tool_results is None:
                continue

            new_results = []
            for tr in msg.tool_results:
                tr_tokens = self._token_counter.count_tokens(tr.content)
                if tr_tokens > 200:  # 只压缩有意义大小的输出
                    stub = (
                        f"[工具输出已压缩: {tr.tool_name}, "
                        f"原始 ~{tr_tokens} tokens]"
                    )
                    new_results.append(ToolCallResult(
                        tool_call_id=tr.tool_call_id,
                        tool_name=tr.tool_name,
                        content=stub,
                    ))
                    compressed_count += 1
                else:
                    new_results.append(tr)

            self._messages[msg_idx] = UnifiedMessage(
                role="user", tool_results=new_results
            )

            # 同步更新 _pending_messages 中对应位置
            if msg_idx < len(self._pending_messages):
                pending = self._pending_messages[msg_idx]
                if pending.get("type") == "tool_result" and pending.get("tool_results"):
                    pending["tool_results"] = [
                        {
                            "tool_call_id": tr.tool_call_id,
                            "tool_name": tr.tool_name,
                            "content": tr.content,
                        }
                        for tr in new_results
                    ]

        if compressed_count > 0:
            new_total = self._token_counter.count_messages_tokens(self._pending_messages)
            _rlog.info(
                f"session_{self.session_id}",
                f"[InfoGathererCtx] 旧轮次压缩: 压缩 {compressed_count} 个工具输出, "
                f"tokens {total_tokens} → {new_total}",
            )

    # ------------------------------------------------------------------
    # 生命周期方法覆写
    # ------------------------------------------------------------------

    async def handle_interruption(self) -> int:
        """中断处理：额外重置 _round_count。"""
        discarded = await super().handle_interruption()
        self._round_count = 0
        return discarded

    async def handle_after_tool_calls(
        self,
        tool_results: list[ToolCallResult],
        loop_task_handler: SessionTaskHandlerV2 | None = None,
        last_call_prompt: str | None = None,
    ) -> list[UnifiedMessage]:
        """工具调用后处理：截断 → 追加（含落盘/冻结循环）→ 压缩旧轮次。"""
        if not tool_results:
            return self._messages

        # Layer 1: 截断过长的单次工具输出
        truncated_results = self._truncate_tool_results(tool_results)

        # 调用父类追加消息（含落盘、冻结循环、页脚注入）
        messages = await super().handle_after_tool_calls(
            truncated_results,
            loop_task_handler=loop_task_handler,
            last_call_prompt=last_call_prompt,
        )

        # Layer 2: 周期性检查总量并压缩旧轮次
        # self._compress_old_rounds()

        return messages

    def _build_tool_list(self):
        """仅返回 ALLOWED_TOOLS 中已启用的工具定义（含摘要工具）。"""
        from ...llm.types import tool_description_to_definition
        from ...tool.tool_manager import ToolManager

        tool_manager = ToolManager.get_instance()
        all_tools_info = tool_manager.get_all_tools_info()

        tool_defs = []
        if all_tools_info:
            for tool_name in all_tools_info:
                tool_instance = tool_manager.get_tool(tool_name)
                if tool_name not in self.ALLOWED_TOOLS:
                    continue
                if not tool_instance.is_enabled():
                    continue
                desc = tool_instance.get_description()
                if not isinstance(desc, dict):
                    continue
                td = tool_description_to_definition(desc)
                if td is not None:
                    tool_defs.append(td)

        tool_defs.append(self._read_tool.get_tool_definition())
        tool_defs.append(self._evict_tool.get_tool_definition())
        return tool_defs
