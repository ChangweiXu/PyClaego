"""Simple Context Handler V2 - 简单上下文策略（V2版本）

基于 BaseContextHandlerV3，使用新的 handle_* 方法替代 get_llm_context。

主要变化：
1. 构造函数需要传入 SessionTaskHandlerV2
2. 使用显式的生命周期方法（handle_before_loop, handle_after_llm_call 等）
3. 废弃 get_llm_context 接口

消息分组机制：
  - 每组对话包含完整的轮次（user → assistant，可选工具调用）
  - 在 handle_before_loop 时机为新的 user 消息分配 group_id
  - 工具调用轮消息和最终 assistant 回复继承当前 group_id
  - 历史消息按 group_id 分组，保留最近 N 组完整对话

历史消息写盘机制：
  handle_before_loop      → 暂存 user 消息，分配新 group_id，返回 LLM 上下文
  handle_after_llm_call   → 暂存 assistant 消息（含工具调用）
  handle_memory_tool_calls → 处理记忆工具调用（如有）
  handle_after_tool_calls → 暂存工具结果
  handle_after_loop       → 批量写盘，清空暂存和 group_id
"""

import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..llm import (
    ReasoningArtifact,
    ToolCall,
    ToolCallResult,
    ToolDefinition,
    UnifiedMessage,
    tool_description_to_definition,
)
from ..logging import get_running_log
from ..task_manager import SessionTaskHandlerV2
from .base_context import BaseContextHandlerV3

_rlog = get_running_log()


class SimpleContextHandlerV2(BaseContextHandlerV3):
    """简单上下文处理器（V2 版，基于 BaseContextHandlerV3）

    继承 BaseContextHandlerV3，使用显式的生命周期方法替代 get_llm_context。

    功能：
    - 从 history.json / history.jsonl 读取并按 group_id 分组，保留最近 N 组对话
    - 边界保护：确保截取后第一条为 user 消息（LLM API 要求）
    - 构建系统提示词
    - 读取 ToolManager 中已启用工具，生成 ToolDefinition 列表
    - 通过 _pending_messages 暂存本轮消息，在 handle_after_loop 时机批量写盘

    适用场景：
    - 简单对话场景
    - 需要工具调用支持
    - 短期上下文记忆
    - 快速原型开发
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ):
        """初始化简单上下文处理器 V2

        Args:
            session_id: 会话 ID
            workspace_path: 工作空间路径
            config: 上下文配置
            session_task_handler: 任务处理器（V2 版本）
        """
        super().__init__(session_id, workspace_path, config, session_task_handler)

        # 从 widget_config 中提取 context 切片再读策略配置
        context_config = self.config  # = config["context"]
        strategy_config = context_config.get("simple", {})
        # 优先使用 keep_groups，回退到 max_messages（自动转换为组数）
        self.keep_groups: int = strategy_config.get(
            "keep_groups",
            strategy_config.get("max_messages", 10) // 2  # 兼容旧配置：10条≈5组
        )
        # 保留 max_messages 以供日志和 get_info() 使用
        self.max_messages: int = strategy_config.get("max_messages", self.keep_groups * 2)

        # 本轮待写盘消息（在 handle_after_loop 时机批量追加到 history 文件）
        self._pending_messages: list[dict[str, Any]] = []

        # 当前对话组 ID（在 handle_before_loop 时机为新的 user 消息分配）
        self._current_group_id: str | None = None

        # 内存中的 UnifiedMessage 列表（用于返回给 Agent）
        self._messages: list[UnifiedMessage] = []

        _rlog.info(
            f"session_{session_id}",
            f"[SimpleContextHandlerV2] 已初始化 (keep_groups={self.keep_groups}, max_messages={self.max_messages})",
        )

    # ------------------------------------------------------------------
    # BaseContextHandlerV3 生命周期方法实现
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: dict[str, Any]) -> dict[str, Any]:
        """在每轮对话开始前准备上下文
        
        Args:
            user_msg: 用户消息 dict
            
        Returns:
            LLM 上下文字典:
            {
                "system": Optional[str],
                "messages": List[UnifiedMessage],
                "tool_list": Optional[List[ToolDefinition]],
            }
        """
        if not self._session_task_handler: 
            raise ValueError("SessionTaskHandlerV2 实例未传入 SessionTaskHandlerV2")

        # 1. 为新的 user 消息生成 group_id
        if "group_id" not in user_msg:
            self._current_group_id = self._generate_group_id()
            user_msg["group_id"] = self._current_group_id
            await self._session_task_handler.log_info(
                f"[SimpleContextHandlerV2] 为新 user 消息分配 group_id: {self._current_group_id}"
            )

        # 2. 暂存 user 消息
        self._pending_messages.append(user_msg)
        await self._session_task_handler.log_info(
            f"[SimpleContextHandlerV2] 暂存 user 消息 (pending={len(self._pending_messages)})"
        )

        # 3. 构建 LLM 上下文
        system: str | None = await self.get_system_prompt()
        self._messages = await self._build_unified_messages()  # ← 初始化内存 messages
        tool_list: list[ToolDefinition] | None = await self._build_tool_list()

        # 4. 将当前 user 消息追加到 _messages（LLM 需要）
        raw_parts = user_msg.get("content_parts")
        if raw_parts:
            content_parts = [_deserialize_content_part(p) for p in raw_parts]
            user_unified = UnifiedMessage(role="user", content_parts=content_parts)
        else:
            user_unified = UnifiedMessage(role="user", text=user_msg.get("content", ""))
        self._messages.append(user_unified)

        await self._session_task_handler.log_info(
            f"[SimpleContextHandlerV2] 上下文构建完成: {len(self._messages)} 条消息, {len(tool_list) if tool_list else 0} 个工具"
        )

        return {
            "system": system,
            "messages": self._messages,  # ← 返回 _messages（包含当前 user 消息）
            "tool_list": tool_list,
        }

    async def handle_after_llm_call(
        self,
        text_reply: str = "",
        tool_calls: list[ToolCall] | None = None,
        reasoning: ReasoningArtifact | None = None,
        produced_by_provider: str | None = None,
        produced_by_model: str | None = None,
    ) -> list[UnifiedMessage]:  # ← 新增返回值
        """LLM 调用后处理（暂存 assistant 消息）
        
        Args:
            text_reply: LLM 文本回复
            tool_calls: 工具调用列表（如有）
            reasoning: provider 思考产物（多态 ReasoningArtifact，必须原样回传）
            produced_by_provider: 生成此消息的 provider 标签（用于跨 provider 切换守卫）
            produced_by_model: 生成此消息的具体模型名（signature 与模型绑定）
        
        Returns:
            更新后的 messages 列表
        """
        if not self._session_task_handler: 
            raise ValueError("SessionTaskHandlerV2 实例未传入 SessionTaskHandlerV2")

        # 构建 assistant 消息
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text_reply or "",
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
        }

        # 附加 group_id
        if self._current_group_id:
            assistant_msg["group_id"] = self._current_group_id

        # 如果有工具调用，附加到消息中
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
        # provider/model 标签（跨 provider 切换守卫所需）
        if produced_by_provider:
            assistant_msg["produced_by_provider"] = produced_by_provider
        if produced_by_model:
            assistant_msg["produced_by_model"] = produced_by_model

        # 暂存消息（用于批量写盘）
        self._pending_messages.append(assistant_msg)

        # 追加到内存 messages
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
            f"[SimpleContextHandlerV2] 暂存 assistant 消息 "
            f"(tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"messages={len(self._messages)}, pending={len(self._pending_messages)})"
        )

        return self._messages  # ← 返回更新后的 messages

    async def handle_memory_tool_calls(
        self,
        tool_calls: list[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any] | None:
        """处理记忆相关的工具调用
        
        SimpleContextHandlerV2 不处理记忆工具，直接返回所有工具调用作为非记忆工具。
        
        Args:
            tool_calls: 所有工具调用
            loop_task_handler: 循环任务处理器
            
        Returns:
            {
                "memory_tool_results": [],  # 记忆工具结果（空）
                "non_memory_calls": tool_calls,  # 所有工具都是非记忆工具
            }
        """
        await loop_task_handler.log_info(
            f"[SimpleContextHandlerV2] 无记忆工具处理，所有 {len(tool_calls)} 个工具都作为普通工具"
        )
        
        return {
            "memory_tool_results": [],
            "non_memory_calls": tool_calls,
        }

    async def handle_after_tool_calls(
        self,
        tool_results: list[ToolCallResult],
        last_call_prompt: str | None = None,
    ) -> list[UnifiedMessage]:  # ← 新增返回值
        """工具调用后处理（暂存工具结果）
        
        Args:
            tool_results: 工具调用结果列表
            last_call_prompt: 可选。最后一轮时传入，附加到消息 content，提示 LLM 直接作答。
        
        Returns:
            更新后的 messages 列表
        """
        if not self._session_task_handler: 
            raise ValueError("SessionTaskHandlerV2 实例未传入 SessionTaskHandlerV2")

        if not tool_results:
            return self._messages  # ← 无工具结果，直接返回

        # 构建 user 消息（包含工具结果）
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

        # 暂存消息（用于批量写盘）
        self._pending_messages.append(user_msg)

        # 追加到内存 messages
        user_unified = UnifiedMessage(
            role="user",
            tool_results=tool_results,
            text=last_call_prompt or None,
        )
        self._messages.append(user_unified)

        await self._session_task_handler.log_info(
            f"[SimpleContextHandlerV2] 暂存工具结果消息 "
            f"(results={len(tool_results)}, messages={len(self._messages)}, pending={len(self._pending_messages)}, "
            f"last_call={bool(last_call_prompt)})"
        )

        return self._messages  # ← 返回更新后的 messages

    async def handle_after_loop(
        self,
        final_message: dict[str, Any],
    ) -> None:
        """对话循环结束后处理（批量写盘）
        
        Args:
            final_message: 最终 assistant 消息
        """
        if not self._session_task_handler: 
            raise ValueError("SessionTaskHandlerV2 实例未传入 SessionTaskHandlerV2")

        # 附加 group_id 到最终消息
        if self._current_group_id and "group_id" not in final_message:
            final_message["group_id"] = self._current_group_id

        # 暂存最终消息（如果尚未暂存）
        # 注意：final_message 可能已经在 handle_after_llm_call 中暂存
        # 这里通过检查内容去重
        if final_message.get("content"):
            # 简单检查：如果 pending 的最后一条不是相同内容，则追加
            should_append = True
            if self._pending_messages:
                last_msg = self._pending_messages[-1]
                if (last_msg.get("role") == "assistant" and 
                    last_msg.get("content") == final_message.get("content")):
                    should_append = False
                    
            if should_append:
                self._pending_messages.append(final_message)

        # 批量写盘
        if self._pending_messages:
            ok = self.history_manager.append_messages(self._pending_messages)
            await self._session_task_handler.log_info(
                f"[SimpleContextHandlerV2] 批量写盘 "
                f"{len(self._pending_messages)} 条消息 (ok={ok})"
            )
            self._pending_messages.clear()
            self._current_group_id = None  # 清空当前组 ID
            self._messages.clear()  # ← 新增：清空内存 messages

    async def handle_spawn_context_snapshot(self) -> dict[str, Any]:
        """获取上下文快照，用于生成子代理"""
        return await BaseContextHandlerV3.handle_spawn_context_snapshot(self)

    # ------------------------------------------------------------------
    # BaseContextHandler 抽象方法实现
    # ------------------------------------------------------------------

    async def get_recent_messages(self, count: int) -> list[dict[str, Any]]:
        """获取最近 count 条消息
        
        Args:
            count: 消息数量（实际返回的消息数量可能少于此值，
                  因为需要保证第一条为 user 消息）
        
        Returns:
            最近的消息列表，第一条保证为 user 消息
        """
        if count <= 0:
            return []
        
        # 从 history 文件读取所有消息
        msgs = self.history_manager.load_all()

        # 按 group_id 分组（旧格式消息会被 in-place 补充推断出的 group_id）
        grouped, dirty = await self._group_messages_by_id(msgs)
        await self._writeback_if_dirty(msgs, dirty)

        # 计算需要保留的组数（count 条消息约等于 count/2 组，向上取整确保不遗漏）
        keep_groups = max(1, (count + 1) // 2)

        # 保留最近 N 组并确保第一条为 user 消息
        return await self._take_recent_groups(grouped, keep_groups)

    async def get_system_prompt(self) -> str | None:
        """获取系统提示词
        
        基于 SIMPLE_V2_SYSTEM_PROMPT 模板，填充 workspace_root 并附加动态技能列表。
        """
        from .system_prompts.simple_v2 import SIMPLE_V2_SYSTEM_PROMPT
        if SIMPLE_V2_SYSTEM_PROMPT is None:
            return None

        prompt = SIMPLE_V2_SYSTEM_PROMPT.format(
            workspace_root=self.workspace_path.absolute().as_posix(),
            project_root=self.widget_config.get("ps_metadata", {}).get(
                "project_root", "."
            ),  # ← 来自 widget resolved_config 的 ps_metadata
        )

        # 附加动态技能列表
        skills_info = await self._get_skills_info()
        if skills_info:
            prompt = prompt + "\n\n---\n\n" + skills_info

        # 附加当前时间（提示 LLM 关注时效性）
        prompt += f"\n\n# 当前时间\n{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        return prompt

    async def _get_skills_info(self) -> str:
        """获取当前 Session 可访问的技能列表（name / path / description）

        Returns:
            格式化的技能信息字符串；无可用技能时返回空字符串
        """
        try:
            from ..skill import get_skill_manager
            skill_manager = get_skill_manager()
            skill_manager.reload_session_skills(self.session_id)  # 确保技能信息最新
            skill_names = skill_manager.list_skills(session_id=self.session_id)

            if not skill_names:
                return ""

            skill_map = skill_manager.get_all_skills(self.session_id)

            lines = ["# 可用技能\n"]
            for i, name in enumerate(skill_names, 1):
                skill = skill_map[name]
                lines.append(f"### {i}. {skill.name}")
                lines.append(f"- **路径**: `{skill.path}`")
                lines.append(f"- **描述**: {skill.description or '（无描述）'}")
                lines.append("")

            lines.append("> 使用技能前，请先读取对应路径下的 `SKILL.md` 获取完整指引。")
            return "\n".join(lines)
        except Exception as e:
            if self._session_task_handler:
                await self._session_task_handler.log_error(
                    f"[SimpleContextHandlerV2] 获取技能信息失败: {e}\n{traceback.format_exc()}"
                )
            else:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[SimpleContextHandlerV2] 获取技能信息失败: {e}\n{traceback.format_exc()}",
                )
            return ""

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _build_unified_messages(self) -> list[UnifiedMessage]:
        """从历史记录构建 UnifiedMessage 列表
        
        Returns:
            UnifiedMessage 列表
        """
        return self.records_to_unified_messages(await self._get_recent_messages())

    async def _get_recent_messages(self) -> list[dict[str, Any]]:
        """获取历史消息，按 group_id 分组后保留最近 N 组
        
        Returns:
            处理后的消息列表（已按组截取，第一条为 user 或列表为空）
        """
        # 从 history 文件读取所有消息
        msgs = self.history_manager.load_all()

        # 按 group_id 分组（旧格式消息会被 in-place 补充推断出的 group_id）
        grouped, dirty = await self._group_messages_by_id(msgs)
        await self._writeback_if_dirty(msgs, dirty)

        # 保留最近 keep_groups 组
        return await self._take_recent_groups(grouped, self.keep_groups)

    async def _build_tool_list(self) -> list[ToolDefinition] | None:
        """从 ToolManager 获取已启用工具，转换为 ToolDefinition 列表

        Returns:
            List[ToolDefinition]（至少有一个工具时）或 None（无工具时）
        """
        try:
            from ..tool import get_tool_manager
            tool_manager = get_tool_manager()
            tool_defs: list[ToolDefinition] = []
            for tool_name in tool_manager.list_loaded_tools():
                tool = tool_manager.get_tool(tool_name)
                if tool and tool.is_enabled():
                    desc = tool.get_description()
                    tool_defs.append(tool_description_to_definition(desc))
            return tool_defs if tool_defs else None
        except Exception as e:
            if self._session_task_handler:
                await self._session_task_handler.log_error(
                    f"[SimpleContextHandlerV2] 构建工具列表失败: {e}\n{traceback.format_exc()}"
                )
            else:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[SimpleContextHandlerV2] 构建工具列表失败: {e}\n{traceback.format_exc()}",
                )
            return None

    # ------------------------------------------------------------------
    # 辅助方法：group_id 生成和消息分组
    # ------------------------------------------------------------------

    async def _writeback_if_dirty(
        self, msgs: list[dict[str, Any]], dirty: bool
    ) -> None:
        """如有消息被补充了 group_id，回写历史文件（一次性迁移）

        Args:
            msgs:  load_all() 返回的完整消息列表（已被 in-place 修改）
            dirty: _group_messages_by_id 返回的脏标志
        """
        if not dirty:
            return
        ok = self.history_manager.save_all(msgs)
        log_msg = f"[SimpleContextHandlerV2] 已将推断的 group_id 回写历史文件 (ok={ok})"
        if self._session_task_handler:
            await self._session_task_handler.log_info(log_msg)
        else:
            _rlog.info(f"session_{self.session_id}", log_msg)

    def _generate_group_id(self) -> str:
        """生成唯一的对话组 ID

        格式: g_{timestamp}_{6位UUID}
        示例: g_20260410_090000_a3f8c1

        Returns:
            str: 唯一的 group_id 字符串
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uuid_suffix = uuid.uuid4().hex[:6]
        return f"g_{timestamp}_{uuid_suffix}"

    async def _group_messages_by_id(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[list[dict[str, Any]]], bool]:
        """将消息列表按 group_id 分组

        对于有 group_id 的消息，直接按 group_id 聚合。
        对于缺失 group_id 的旧格式消息，通过角色转换推断分组边界：
        只有 role==user 且 type!=tool_result 的消息才开启新组，
        后续 assistant / tool_result 消息继承当前组，与原始写盘逻辑保持一致。
        推断出的 group_id 会直接写入消息 dict（in-place），调用方根据 dirty 标志
        决定是否将修改后的 messages 回写到历史文件。

        Args:
            messages: 历史消息列表（可能被 in-place 修改）

        Returns:
            (groups, dirty):
              groups — 分组后的消息列表，每个子列表是一个完整的对话组
              dirty  — 是否有消息被补充了 group_id（True 表示需要回写历史文件）
        """
        groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        last_group_id: str | None = None  # 最近一次确定的组 ID（真实或推断）
        dirty: bool = False

        for msg in messages:
            group_id = msg.get("group_id")

            if group_id is not None:
                # 有明确 group_id：直接使用，并更新 last_group_id 供后续无 group_id 消息继承
                last_group_id = group_id
            else:
                # 旧格式消息：按角色转换推断分组边界
                # 只有真正的新用户轮次（非工具结果）才开启新组；
                # assistant / tool_result 消息继承 last_group_id（无论来自真实还是推断的组）
                is_new_turn = (
                    msg.get("role") == "user"
                    and msg.get("type") != "tool_result"
                )
                if is_new_turn or last_group_id is None:
                    last_group_id = f"g_legacy_{uuid.uuid4().hex[:6]}"
                    if self._session_task_handler:
                        await self._session_task_handler.log_warning(
                            f"[SimpleContextHandlerV2] 消息缺失 group_id，推断新组 ID: {last_group_id}"
                        )
                    else:
                        _rlog.warning(
                            f"session_{self.session_id}",
                            f"[SimpleContextHandlerV2] 消息缺失 group_id，推断新组 ID: {last_group_id}",
                        )
                group_id = last_group_id
                msg["group_id"] = group_id  # 回填到消息 dict（in-place）
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
        """保留最近 N 组消息，并确保第一条为 user 消息

        Args:
            grouped_messages: 已分组的消息列表
            keep_groups: 保留的组数

        Returns:
            扁平化的消息列表，第一条保证是 user 消息
        """
        if not grouped_messages:
            return []

        # 保留最近 N 组
        recent_groups = grouped_messages[-keep_groups:] if len(grouped_messages) > keep_groups else grouped_messages

        # 扁平化
        flattened: list[dict[str, Any]] = []
        for group in recent_groups:
            flattened.extend(group)

        # 边界保护：确保第一条为 user 消息
        while flattened and flattened[0].get("role") != "user":
            if self._session_task_handler:
                await self._session_task_handler.log_warning(
                    f"[SimpleContextHandlerV2] 截取后第一条非 user 消息，丢弃: {flattened[0].get('role')}"
                )
            else:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[SimpleContextHandlerV2] 截取后第一条非 user 消息，丢弃: {flattened[0].get('role')}",
                )
            flattened.pop(0)

        return flattened

    def get_info(self) -> dict[str, Any]:
        """获取上下文处理器信息

        Returns:
            信息字典
        """
        info = super().get_info()
        info.update({
            "keep_groups": self.keep_groups,
            "max_messages": self.max_messages,
            "pending_messages": len(self._pending_messages),
            "current_group_id": self._current_group_id,
            "history_manager": self.history_manager.get_info(),
        })
        return info


# ─────────────────────────────────────────────────────────────────
#  模块级辅助函数
# ─────────────────────────────────────────────────────────────────

def _deserialize_content_part(d: dict):
    return BaseContextHandlerV3.deserialize_content_part(d)
