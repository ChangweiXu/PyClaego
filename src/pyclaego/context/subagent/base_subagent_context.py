"""BaseSubAgentContextHandler — 子 Agent 专属上下文处理器基类（V3 生命周期版）

与普通 Context 的关键区别：
  - 不从磁盘恢复已有历史，__init__ 时由参数构建初始消息
  - 工具集仅来自 ToolManager（天然不含 Memory 工具和 Agent 工具）
  - 支持两种 memory_mode：
      "empty"   : 仅子 Agent 专属系统提示词，历史消息为空
      "inherit" : 使用 SpawnSubagentTool 传入的父快照处理结果作为初始消息

写盘机制：
  handle_before_loop    → 暂存 user 消息，返回完整上下文
  handle_after_llm_call → 暂存 assistant 消息
  handle_after_tool_calls → 暂存工具结果消息（如有）
  handle_after_loop     → 批量写盘并清空暂存
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from ..base_context import BaseContextHandlerV3
from ...llm import ReasoningArtifact, UnifiedMessage, ToolCall, ToolCallResult
from ...task_manager import SessionTaskHandlerV2
from ...logging import get_running_log

_rlog = get_running_log()

# 子 Agent 专属系统提示词模板
_SUBAGENT_SYSTEM_PROMPT = """# Sub-Agent Identity
你是 PyClaw-CC 的子 Agent（子任务执行者）。
你被主 Agent 创建，负责完成一个具体的子任务。

# 工作目录
当前工作目录：`{workspace_path}`
你拥有该目录的完整读写权限，可以在其中创建中间笔记和下载文件。

# 任务完成
完成任务后，将成果、结论以及任何重要信息写在**最后一条回复**里。
系统会自动将你的最终回复保存为报告，供主 Agent 读取——无需手动写入任何报告文件。

# Security
使用工具操作文件时，路径必须在工作目录下进行。
"""


class BaseSubAgentContextHandler(BaseContextHandlerV3):
    """子 Agent 专属上下文处理器（V3 生命周期）"""

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: Dict[str, Any],
        memory_mode: str = "empty",
        initial_messages: Optional[List[UnifiedMessage]] = None,
        initial_system: Optional[str] = None,
        session_task_handler: Optional[SessionTaskHandlerV2] = None,
    ) -> None:
        if not session_task_handler:
            raise ValueError("必须提供 SessionTaskHandlerV2 实例")

        super().__init__(session_id, workspace_path, config, session_task_handler)

        self.memory_mode: str = memory_mode
        self._subagent_system: str = (
            initial_system
            if initial_system is not None
            else _SUBAGENT_SYSTEM_PROMPT.format(workspace_path=str(workspace_path))
        )

        # inherit 模式下的初始消息
        self._initial_messages: List[UnifiedMessage] = list(initial_messages or [])

        # 本轮待写盘消息（dict 结构）
        self._pending_messages: List[Dict[str, Any]] = []

        # 内存中的 UnifiedMessage 链
        self._messages: List[UnifiedMessage] = []

        _rlog.info(
            f"session_{session_id}",
            f"[BaseSubAgentContextHandler] 初始化完成 "
            f"(memory_mode={memory_mode}, initial_messages={len(self._initial_messages)}, "
            f"workspace={workspace_path})",
        )

    # ------------------------------------------------------------------
    # BaseContextHandlerV3 生命周期方法实现
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: Dict[str, Any]) -> Dict[str, Any]:
        """在每轮对话开始前准备上下文"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        self._pending_messages.append(user_msg)

        # 每轮以 initial_messages 作为基线；对子 Agent（单次执行）已足够
        self._messages = list(self._initial_messages)
        self._messages.append(
            UnifiedMessage(role="user", text=user_msg.get("content", ""))
        )

        await self._session_task_handler.log_info(
            f"[SubAgentCtxV3] handle_before_loop: pending={len(self._pending_messages)}, "
            f"messages={len(self._messages)}"
        )

        return {
            "system": self._subagent_system,
            "messages": self._messages,
            "tool_list": self._build_tool_list(),  # TODO 检查工具列表
        }

    async def handle_after_llm_call(
        self,
        text_reply: str = "",
        tool_calls: Optional[List[ToolCall]] = None,
        reasoning: Optional[ReasoningArtifact] = None,
        produced_by_provider: Optional[str] = None,
        produced_by_model: Optional[str] = None,
    ) -> List[UnifiedMessage]:
        """LLM 调用后处理（暂存 assistant 消息）"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": text_reply or "",
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
        }

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
            f"[SubAgentCtxV3] handle_after_llm_call: "
            f"tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"pending={len(self._pending_messages)}"
        )

        return self._messages

    async def handle_memory_tool_calls(
        self,
        tool_calls: List[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> Optional[Dict[str, Any]]:
        """子 Agent Context 默认不处理记忆工具，全部透传。"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        await self._session_task_handler.log_info(
            f"[SubAgentCtxV3] 无记忆工具处理，透传 {len(tool_calls)} 个工具调用"
        )
        return {
            "memory_tool_results": [],
            "non_memory_calls": tool_calls,
        }

    async def handle_after_tool_calls(
        self,
        tool_results: List[ToolCallResult],
        last_call_prompt: Optional[str] = None,
    ) -> List[UnifiedMessage]:
        """工具调用后处理（暂存工具结果消息）"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        if not tool_results:
            return self._messages

        user_msg: Dict[str, Any] = {
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
        self._messages.append(UnifiedMessage(
            role="user",
            tool_results=tool_results,
            text=last_call_prompt or None,
        ))

        await self._session_task_handler.log_info(
            f"[SubAgentCtxV3] handle_after_tool_calls: "
            f"results={len(tool_results)}, pending={len(self._pending_messages)}, "
            f"last_call={bool(last_call_prompt)}"
        )

        return self._messages

    async def handle_after_loop(
        self,
        final_message: Dict[str, Any],
    ) -> None:
        """循环结束后收尾：去重追加 + 批量写盘"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

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
                f"[SubAgentCtxV3] 批量写盘 {len(self._pending_messages)} 条 (ok={ok})"
            )
            self._pending_messages.clear()
            self._messages.clear()

    # ------------------------------------------------------------------
    # 工具与基础方法
    # ------------------------------------------------------------------

    def _build_tool_list(self):
        return None

    def get_recent_messages(self, count: int) -> List[Dict[str, Any]]:
        """获取最近历史消息。"""
        if count <= 0:
            return []
        return self.history_manager.load_recent(count)
