"""Window Context Handler V2 - 滑动窗口上下文策略（V3生命周期版本）

基于 BaseContextHandlerV3，使用新的 handle_* 生命周期方法替代 get_llm_context。

设计目标：
1. 保持 window_context.py 的“最简滑动窗口”语义
2. 仅保留最近 N 组问答（近似 keep_groups * 2 条消息）
3. 不提供工具列表（tool_list 固定为 None）
4. 兼容含 tool_results 的 user 消息，确保消息链完整

历史消息写盘机制：
  handle_before_loop       → 暂存 user 消息，返回窗口上下文
  handle_after_llm_call    → 暂存 assistant 消息（含可选工具调用）
  handle_memory_tool_calls → 不做记忆工具处理，透传给普通工具流程
  handle_after_tool_calls  → 暂存工具结果消息（user.tool_results）
  handle_after_loop        → 批量写盘，清空暂存
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from ..llm import ReasoningArtifact, ToolCall, ToolCallResult, UnifiedMessage
from ..logging import get_running_log
from ..task_manager import SessionTaskHandlerV2
from .base_context import BaseContextHandlerV3

_rlog = get_running_log()


class WindowContextHandlerV2(BaseContextHandlerV3):
    """滑动窗口上下文处理器（V3 生命周期版）

    功能：
    - 从 history 文件读取最近窗口消息
    - 构建 UnifiedMessage 上下文
    - 维护本轮 pending 消息并在 loop 结束时批量写盘
    - tool_list 固定为 None（不主动加载工具定义）
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        """初始化滑动窗口上下文处理器 V2

        Args:
            session_id: 会话 ID
            workspace_path: 工作空间路径
            config: 上下文配置（读取 window.keep_groups）
            session_task_handler: 任务处理器（V2）
        """
        super().__init__(session_id, workspace_path, config, session_task_handler)

        # config 是完整 widget_config；self.config 已是 context 切片
        window_cfg = self.config.get("window", {})
        self.keep_groups: int = window_cfg.get("keep_groups", 5)

        # 本轮待写盘消息（在 handle_after_loop 时机批量追加到 history 文件）
        self._pending_messages: list[dict[str, Any]] = []

        # 内存中的 UnifiedMessage 列表（用于返回给 Agent）
        self._messages: list[UnifiedMessage] = []

        _rlog.info(
            f"session_{session_id}",
            f"[WindowContextHandlerV2] 已初始化 (keep_groups={self.keep_groups})",
        )

    # ------------------------------------------------------------------
    # BaseContextHandlerV3 生命周期方法实现
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: dict[str, Any]) -> dict[str, Any]:
        """在每轮对话开始前准备窗口上下文

        Args:
            user_msg: 用户消息 dict

        Returns:
            LLM 上下文字典: {"system", "messages", "tool_list"}
        """
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        # 1) 暂存当前 user 消息
        self._pending_messages.append(user_msg)
        await self._session_task_handler.log_info(
            f"[WindowContextHandlerV2] 暂存 user 消息 (pending={len(self._pending_messages)})"
        )

        # 2) 构建历史窗口 messages
        system: str | None = self.get_system_prompt()
        self._messages = self._build_unified_messages()

        # 3) 将当前 user 消息追加到 messages（供本轮 LLM 使用）
        raw_parts = user_msg.get("content_parts")
        if raw_parts:
            content_parts = [_deserialize_content_part(p) for p in raw_parts]
            user_unified = UnifiedMessage(role="user", content_parts=content_parts)
        else:
            user_unified = UnifiedMessage(role="user", text=user_msg.get("content", ""))
        self._messages.append(user_unified)

        await self._session_task_handler.log_info(
            f"[WindowContextHandlerV2] 上下文构建完成: {len(self._messages)} 条消息"
        )

        return {
            "system": system,
            "messages": self._messages,
            "tool_list": None,
        }

    async def handle_after_llm_call(
        self,
        text_reply: str = "",
        tool_calls: list[ToolCall] | None = None,
        reasoning: ReasoningArtifact | None = None,
        produced_by_provider: str | None = None,
        produced_by_model: str | None = None,
    ) -> list[UnifiedMessage]:
        """LLM 调用后处理（暂存 assistant 消息）"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text_reply or "",
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
        }

        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
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

        assistant_unified = UnifiedMessage(
            role="assistant",
            text=text_reply,
            tool_calls=tool_calls,
            reasoning=reasoning,
            produced_by_provider=produced_by_provider,
            produced_by_model=produced_by_model,
        )
        self._messages.append(assistant_unified)

        await self._session_task_handler.log_info(
            f"[WindowContextHandlerV2] 暂存 assistant 消息 "
            f"(tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"messages={len(self._messages)}, pending={len(self._pending_messages)})"
        )

        return self._messages

    async def handle_memory_tool_calls(
        self,
        tool_calls: list[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any] | None:
        """处理记忆相关工具调用

        WindowContextHandlerV2 不处理记忆工具，直接透传所有工具调用。
        """
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        await self._session_task_handler.log_info(
            f"[WindowContextHandlerV2] 无记忆工具处理，所有 {len(tool_calls)} 个工具都作为普通工具"
        )

        return {
            "memory_tool_results": [],
            "non_memory_calls": tool_calls,
        }

    async def handle_after_tool_calls(
        self,
        tool_results: list[ToolCallResult],
        last_call_prompt: str | None = None,
    ) -> list[UnifiedMessage]:
        """工具调用后处理（暂存工具结果）"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

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

        self._pending_messages.append(user_msg)

        user_unified = UnifiedMessage(
            role="user",
            tool_results=tool_results,
            text=last_call_prompt or None,
        )
        self._messages.append(user_unified)

        await self._session_task_handler.log_info(
            f"[WindowContextHandlerV2] 暂存工具结果消息 "
            f"(results={len(tool_results)}, messages={len(self._messages)}, pending={len(self._pending_messages)}, "
            f"last_call={bool(last_call_prompt)})"
        )

        return self._messages

    async def handle_after_loop(
        self,
        final_message: dict[str, Any],
    ) -> None:
        """对话循环结束后处理（批量写盘）"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        # final_message 可能已在 handle_after_llm_call 中暂存，这里做简单去重
        if final_message.get("content"):
            should_append = True
            if self._pending_messages:
                last_msg = self._pending_messages[-1]
                if (
                    last_msg.get("role") == "assistant"
                    and last_msg.get("content") == final_message.get("content")
                ):
                    should_append = False

            if should_append:
                self._pending_messages.append(final_message)

        if self._pending_messages:
            ok = self.history_manager.append_messages(self._pending_messages)
            await self._session_task_handler.log_info(
                f"[WindowContextHandlerV2] 批量写盘 "
                f"{len(self._pending_messages)} 条消息 (ok={ok})"
            )

            self._pending_messages.clear()
            self._messages.clear()

    # ------------------------------------------------------------------
    # BaseContextHandler 抽象方法实现
    # ------------------------------------------------------------------

    def get_recent_messages(self, count: int) -> list[dict[str, Any]]:
        """获取最近 count 条消息，并确保第一条为 user 消息

        Args:
            count: 最多读取的消息条数

        Returns:
            处理后的消息列表（第一条保证为 user 或列表为空）
        """
        if count <= 0:
            return []

        msgs = self.history_manager.load_recent(count)

        while msgs and msgs[0].get("role") != "user":
            msgs.pop(0)

        return msgs

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_unified_messages(self) -> list[UnifiedMessage]:
        """从历史记录构建 UnifiedMessage 列表（窗口裁剪语义）"""
        return self.records_to_unified_messages(
            self.get_recent_messages(self.keep_groups * 2)
        )

    def get_info(self) -> dict[str, Any]:
        """获取上下文处理器信息"""
        info = super().get_info()
        info.update(
            {
                "keep_groups": self.keep_groups,
                "pending_messages": len(self._pending_messages),
                "history_manager": self.history_manager.get_info(),
            }
        )
        return info


def _deserialize_content_part(d: dict):
    from .base_context import BaseContextHandlerV3
    return BaseContextHandlerV3.deserialize_content_part(d)
