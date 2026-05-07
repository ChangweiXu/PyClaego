"""agent_pipeline 上下文处理器

BasePipelineContextHandler
  在 BaseContextHandlerV3 基础上新增 clone_for_subagent() 协议，
  提供默认实现（子类可覆盖以实现更精确的 history/memory 隔离）。

PipelineWindowContextHandler
  简单滑动窗口实现：
    - 保留最近 keep_groups 组对话（以 group_id 为单位）
    - 从 ToolManager 加载全局工具列表（不含记忆工具）
    - 完整实现 BaseContextHandlerV3 V3 生命周期
    - 作为验证 Pipeline 框架正确性的最简上下文策略
  配置键：context.pipeline_window.keep_groups（默认 5）
"""
from __future__ import annotations

import uuid
from abc import abstractmethod
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

_rlog = get_running_log()


# ===========================================================================
# BasePipelineContextHandler
# ===========================================================================

class BasePipelineContextHandler(BaseContextHandlerV3):
    """Pipeline 专用上下文处理器基类。

    在 BaseContextHandlerV3 基础上增加 clone_for_subagent() 方法：
    当主代理需要 fork 子代理时，调用此方法获得一个指向独立 workspace
    的新 context 实例，避免子代理污染主代理的 history / memory。

    默认实现以相同 widget_config 创建新实例，仅更换 workspace_path
    和 session_id。子类应根据自身需求覆盖此方法（例如 SoulV5/V6 需要
    同时隔离 MemoryManager namespace）。
    """

    async def clone_for_subagent(
        self,
        subagent_workspace: Path,
        subagent_session_id: str,
        memory_mode: str = "empty",
        initial_messages: list[UnifiedMessage] | None = None,
        initial_system: str | None = None,
    ) -> BasePipelineContextHandler:
        """创建子代理专用的上下文处理器副本。

        Args:
            subagent_workspace:   子代理独立工作目录（已由调用方创建）
            subagent_session_id:  子代理唯一会话 ID
            memory_mode:          "empty"（从零开始）或 "inherit"（继承父消息）
            initial_messages:     inherit 模式时，父代理传入的初始消息
            initial_system:       inherit 模式时，父代理的系统提示词

        Returns:
            新的同类 context 实例，指向独立 workspace
        """
        instance = self.__class__(
            session_id=subagent_session_id,
            workspace_path=subagent_workspace,
            config=self.widget_config,
        )
        return instance

    @abstractmethod
    async def handle_before_loop(self, user_msg: dict[str, Any], max_rounds: int) -> dict[str, Any]:
        """对话开始前准备上下文（返回 system / messages / tool_list）。"""
        pass


# ===========================================================================
# PipelineWindowContextHandler
# ===========================================================================

class PipelineWindowContextHandler(BasePipelineContextHandler):
    """简单滑动窗口上下文处理器（Pipeline 专用）。

    特性：
      - 保留最近 keep_groups 组对话历史
      - 工具列表从全局 ToolManager 动态加载（不含 memory / agent 工具）
      - 不实现记忆系统，handle_memory_tool_calls 直接透传所有调用
      - 支持 clone_for_subagent（默认：空历史新实例）

    配置路径：widget_config["context"]["pipeline_window"]
      keep_groups: int = 5
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        super().__init__(session_id, workspace_path, config, session_task_handler)

        # self.config 已由基类绑定到 config["context"] 切片
        win_cfg: dict[str, Any] = self.config.get("pipeline_window", {})
        self.keep_groups: int = int(win_cfg.get("keep_groups", 5))

        # 本轮待写盘消息（handle_after_loop 批量写盘）
        self._pending_messages: list[dict[str, Any]] = []

        # 内存中的 UnifiedMessage 链（每轮 handle_before_loop 重建）
        self._messages: list[UnifiedMessage] = []

        # 当前对话组 ID（handle_before_loop 分配，同组消息共享）
        self._current_group_id: str | None = None

        _rlog.info(
            f"session_{session_id}",
            f"[PipelineWindowContextHandler] 初始化 (keep_groups={self.keep_groups})",
        )

    # ------------------------------------------------------------------
    # BaseContextHandlerV3 生命周期实现
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: dict[str, Any], max_rounds: int) -> dict[str, Any]:
        """对话开始前准备上下文（返回 system / messages / tool_list）。"""
        if not self._session_task_handler:
            raise ValueError("[PipelineWindowContextHandler] SessionTaskHandlerV2 未设置")

        # 为新的 user 消息生成 group_id
        if "group_id" not in user_msg:
            self._current_group_id = self._generate_group_id()
            user_msg["group_id"] = self._current_group_id
            await self._session_task_handler.log_info(
                f"[PipelineWindowContextHandler] 为新 user 消息分配 group_id: {self._current_group_id}"
            )

        # 暂存 user 消息
        self._pending_messages.append(user_msg)

        # 构建历史窗口（按组保留）
        system: str | None = await self.get_system_prompt()
        self._messages = await self._build_window_messages()

        # 追加当前 user 消息
        raw_parts = user_msg.get("content_parts")
        if raw_parts:
            content_parts = [
                BaseContextHandlerV3.deserialize_content_part(p) for p in raw_parts
            ]
            user_unified = UnifiedMessage(role="user", content_parts=content_parts)
        else:
            user_unified = UnifiedMessage(role="user", text=user_msg.get("content", ""))
        self._messages.append(user_unified)

        tool_list = self._build_tool_list()

        await self._session_task_handler.log_info(
            f"[PipelineWindowContextHandler] 上下文就绪: "
            f"{len(self._messages)} msgs, {len(tool_list)} tools"
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
        """暂存 assistant 消息，返回更新后的 messages 列表。"""
        assistant_msg: dict[str, Any] = {
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
        if reasoning:
            assistant_msg["reasoning"] = reasoning.to_dict()
        if produced_by_provider:
            assistant_msg["produced_by_provider"] = produced_by_provider
        if produced_by_model:
            assistant_msg["produced_by_model"] = produced_by_model

        # 附加 group_id
        if self._current_group_id:
            assistant_msg["group_id"] = self._current_group_id

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
        return self._messages

    async def handle_memory_tool_calls(
        self,
        tool_calls: list[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any] | None:
        """无记忆工具，所有调用透传为普通工具。"""
        return {
            "memory_tool_results": [],
            "non_memory_calls": tool_calls,
        }

    async def handle_after_tool_calls(
        self,
        tool_results: list[ToolCallResult],
        last_call_prompt: str | None = None,
        loop_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> list[UnifiedMessage]:
        """暂存工具结果消息，返回更新后的 messages 列表。"""
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

        # 附加 group_id
        if self._current_group_id:
            user_msg["group_id"] = self._current_group_id

        self._pending_messages.append(user_msg)

        user_unified = UnifiedMessage(
            role="user",
            tool_results=tool_results,
            text=last_call_prompt or None,
        )
        self._messages.append(user_unified)
        return self._messages

    async def handle_after_loop(self, final_message: dict[str, Any]) -> None:
        """批量写盘所有暂存消息，清空内存状态。"""
        # 附加 group_id 到最终消息
        if self._current_group_id and "group_id" not in final_message:
            final_message["group_id"] = self._current_group_id

        # 末尾消息去重（avoid double-writing the final assistant message）
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

        if self._pending_messages:
            ok = self.history_manager.append_messages(self._pending_messages)
            if self._session_task_handler:
                await self._session_task_handler.log_info(
                    f"[PipelineWindowContextHandler] 写盘 "
                    f"{len(self._pending_messages)} 条消息 (ok={ok})"
                )

        self._pending_messages.clear()
        self._messages.clear()
        self._current_group_id = None  # 清空当前组 ID

    # ------------------------------------------------------------------
    # BaseContextHandler 抽象方法（get_recent_messages）
    # ------------------------------------------------------------------

    async def get_recent_messages(self, count: int) -> list[dict[str, Any]]:
        """从历史文件读取最近消息，按 group_id 分组后保留最近 keep_groups 组。

        确保返回的第一条消息为 user 角色。
        对于缺失 group_id 的旧消息，会推断并回写（一次性迁移）。
        """
        if count <= 0:
            return []
        msgs = self.history_manager.load_all()
        # 按 group_id 分组（旧格式消息会被 in-place 补充推断出的 group_id）
        grouped, dirty = await self._group_messages_by_id(msgs)
        await self._writeback_if_dirty(msgs, dirty)
        # 保留最近 keep_groups 组
        recent = self._take_recent_groups(grouped, self.keep_groups)
        # 保证第一条为 user 消息
        while recent and recent[0].get("role") != "user":
            recent.pop(0)
        return recent

    # ------------------------------------------------------------------
    # 系统提示词构建
    # ------------------------------------------------------------------

    async def get_system_prompt(self, user_text: str = "") -> str | None:
        """构建系统提示词：基础模板 + 技能列表 + 当前时间。

        覆盖 BaseContextHandlerV3.get_system_prompt()，
        提供与 SoulV6 同等丰富的 LLM 行为引导。
        """
        from ..system_prompts.pipeline_v1 import PIPELINE_V1_SYSTEM_PROMPT

        prompt = PIPELINE_V1_SYSTEM_PROMPT.format(
            workspace_root=self.workspace_path.absolute().as_posix(),
            project_root=self.widget_config.get("ps_metadata", {}).get(
                "project_root", "."
            ),
        )

        # 注入可用技能列表
        skills_info = await self._get_skills_info()
        if skills_info:
            prompt += "\n\n---\n\n" + skills_info

        # 当前时间
        from datetime import datetime, timezone
        prompt += (
            f"\n\n# 当前时间\n"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        )

        return prompt

    async def _get_skills_info(self) -> str:
        """获取可用技能列表（复用 SkillManager 接口）。"""
        try:
            from ...skill import get_skill_manager

            skill_manager = get_skill_manager()
            skill_manager.reload_session_skills(self.session_id)
            skill_names = skill_manager.list_skills(session_id=self.session_id)

            if not skill_names:
                return ""

            skill_map = skill_manager.get_all_skills(self.session_id)
            lines = ["# 可用技能\n"]
            for i, name in enumerate(skill_names, 1):
                skill = skill_map[name]
                lines.append(f"### {i}. {skill.name}")
                lines.append(f"- **路径**: `{skill.path}`")
                lines.append(
                    f"- **描述**: {skill.description or '（无描述）'}"
                )
                lines.append("")
            lines.append(
                "> 使用技能前，请先读取对应路径下的 `SKILL.md` 获取完整指引。"
            )
            return "\n".join(lines)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[PipelineWindowContextHandler] 获取技能信息失败: {e}",
            )
            return ""

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _generate_group_id(self) -> str:
        """生成唯一的对话组 ID。

        格式: g_{YYYYMMDD_HHMMSS}_{6位UUID}
        示例: g_20260502_143025_a3f8c1
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uuid_suffix = uuid.uuid4().hex[:6]
        return f"g_{timestamp}_{uuid_suffix}"

    async def _writeback_if_dirty(
        self, msgs: list[dict[str, Any]], dirty: bool
    ) -> None:
        """如有消息被补充了 group_id，回写历史文件（一次性迁移）。"""
        if not dirty:
            return
        ok = self.history_manager.save_all(msgs)
        log_msg = (
            f"[PipelineWindowContextHandler] 已将推断的 group_id 回写历史文件 (ok={ok})"
        )
        if self._session_task_handler:
            await self._session_task_handler.log_info(log_msg)
        else:
            _rlog.info(f"session_{self.session_id}", log_msg)

    async def _group_messages_by_id(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[list[dict[str, Any]]], bool]:
        """将消息列表按 group_id 分组。

        对于有 group_id 的消息，直接按 group_id 聚合。
        对于缺失 group_id 的旧格式消息，通过角色转换推断分组边界：
        只有 role==user 且 type!=tool_result 的消息才开启新组，
        后续 assistant / tool_result 消息继承当前组。
        推断出的 group_id 会直接写入消息 dict（in-place），调用方根据 dirty 标志
        决定是否将修改后的 messages 回写到历史文件。

        Returns:
            (groups, dirty):
              groups — 分组后的消息列表，每个子列表是一个完整的对话组
              dirty  — 是否有消息被补充了 group_id（True 表示需要回写历史文件）
        """
        from collections import OrderedDict

        groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        last_group_id: str | None = None
        dirty: bool = False

        for msg in messages:
            group_id = msg.get("group_id")

            if group_id is not None:
                last_group_id = group_id
            else:
                # 旧格式消息：按角色转换推断分组边界
                is_new_turn = (
                    msg.get("role") == "user"
                    and msg.get("type") != "tool_result"
                )
                if is_new_turn or last_group_id is None:
                    last_group_id = f"g_legacy_{uuid.uuid4().hex[:6]}"
                    if self._session_task_handler:
                        await self._session_task_handler.log_warning(
                            f"[PipelineWindowContextHandler] 消息缺失 group_id，推断新组 ID: {last_group_id}"
                        )
                    else:
                        _rlog.warning(
                            f"session_{self.session_id}",
                            f"[PipelineWindowContextHandler] 消息缺失 group_id，推断新组 ID: {last_group_id}",
                        )
                group_id = last_group_id
                msg["group_id"] = group_id
                dirty = True

            if group_id not in groups:
                groups[group_id] = []
            groups[group_id].append(msg)

        return list(groups.values()), dirty

    def _take_recent_groups(
        self, groups: list[list[dict[str, Any]]], keep: int
    ) -> list[dict[str, Any]]:
        """从分组消息中保留最近 keep 组，展平为一维列表。"""
        if not groups:
            return []
        recent = groups[-keep:] if len(groups) > keep else groups
        return [msg for group in recent for msg in group]

    async def _build_window_messages(self) -> list[UnifiedMessage]:
        """从历史记录构建 UnifiedMessage 窗口列表（按组保留）。"""
        raw = await self.get_recent_messages(self.keep_groups)
        return self.records_to_unified_messages(raw)

    def _build_tool_list(self) -> list[ToolDefinition]:
        """从全局 ToolManager 加载工具定义列表。

        使用公共接口（list_loaded_tools + get_tool）而非内部 _tools 字典。
        遇到单个工具转换失败时记录警告并跳过，不影响整体。
        """
        from ...tool import get_tool_manager

        tool_manager = get_tool_manager()
        tool_defs: list[ToolDefinition] = []

        for tool_name in tool_manager.list_loaded_tools():
            tool_inst = tool_manager.get_tool(tool_name)
            if tool_inst is None:
                continue
            try:
                desc = tool_inst.get_description()
                tool_defs.append(tool_description_to_definition(desc))
            except Exception as exc:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[PipelineWindowContextHandler] 跳过工具 '{tool_name}': {exc}",
                )

        return tool_defs
