"""SoulV5ContextHandler — 多层记忆上下文处理器

继承 BaseContextHandlerV3，使用 SoulV5MemoryManager 管理多层记忆文件树。

生命周期：
  handle_before_loop  → 初始化 manager / 加载偏好 / 加载历史 groups / 构建工具列表
  handle_after_llm_call → 暂存 assistant 消息
  handle_memory_tool_calls → 拦截并执行记忆工具，非记忆工具透传
  handle_after_tool_calls → 暂存工具结果
  handle_after_loop   → 持久化 group + 批量写盘 + 触发自动压缩（if needed）
  handle_compress     → 手动 /compress 命令入口

记忆工具：
  soulv5_memory_query / soulv5_memory_read / soulv5_memory_save_case /
  soulv5_memory_save_experience / soulv5_memory_update /
  soulv5_memory_browse_topics / soulv5_memory_update_preferences /
  soulv5_memory_deprecate
"""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base_context import BaseContextHandlerV3
from .soulv5_memory_manager import SoulV5MemoryManager
from .token_counter import TokenCounter
from ..llm import (
    UnifiedMessage,
    ToolCall,
    ToolCallResult,
    ToolDefinition,
    ReasoningArtifact,
    tool_description_to_definition,
)
from ..llm.types import ContentPart, TextPart, ImagePart
from ..task_manager import SessionTaskHandlerV2
from ..logging import get_running_log

_rlog = get_running_log()


# ---------------------------------------------------------------------------
# 记忆工具前缀
# ---------------------------------------------------------------------------
_SOULV5_TOOL_PREFIX = "soulv5_memory_"

# 工具名 → 类的映射（延迟导入，避免循环引用）
_TOOL_REGISTRY: Dict[str, str] = {
    "soulv5_memory_query":              "SoulV5QueryTool",
    "soulv5_memory_read":               "SoulV5ReadTool",
    "soulv5_memory_save_case":          "SoulV5SaveCaseTool",
    "soulv5_memory_save_experience":    "SoulV5SaveExperienceTool",
    "soulv5_memory_update":             "SoulV5UpdateTool",
    "soulv5_memory_browse_topics":      "SoulV5BrowseTopicsTool",
    "soulv5_memory_update_preferences": "SoulV5UpdatePreferencesTool",
    "soulv5_memory_deprecate":          "SoulV5DeprecateTool",
}

# 工具类所在模块映射
_TOOL_MODULE_MAP: Dict[str, str] = {
    "SoulV5QueryTool":              ".memory_tools.soulv5_query_tool",
    "SoulV5ReadTool":               ".memory_tools.soulv5_read_tool",
    "SoulV5SaveCaseTool":           ".memory_tools.soulv5_save_case_tool",
    "SoulV5SaveExperienceTool":     ".memory_tools.soulv5_save_experience_tool",
    "SoulV5UpdateTool":             ".memory_tools.soulv5_update_tool",
    "SoulV5BrowseTopicsTool":       ".memory_tools.soulv5_browse_topics_tool",
    "SoulV5UpdatePreferencesTool":  ".memory_tools.soulv5_preferences_tool",
    "SoulV5DeprecateTool":          ".memory_tools.soulv5_deprecate_tool",
}


def _deserialize_content_part(data: Dict[str, Any]) -> ContentPart:
    """将序列化的 content_part dict 还原为 ContentPart"""
    part_type = data.get("type", "text")
    if part_type == "image":
        return ImagePart(data=data.get("data", ""), media_type=data.get("media_type", ""))
    return TextPart(text=data.get("text", ""))


class SoulV5ContextHandler(BaseContextHandlerV3):
    """多层记忆上下文处理器

    使用 SoulV5MemoryManager 管理 MD 文件树 + SQLite 索引，
    通过记忆工具让 LLM 在对话中读写记忆。
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: Dict[str, Any],
        session_task_handler: Optional[SessionTaskHandlerV2] = None,
    ):
        super().__init__(session_id, workspace_path, config, session_task_handler)

        strategy_config = config.get("soul_v5", {})

        # 历史保留（兼容 simple_v2 的 keep_groups 语义）
        self.keep_groups: int = strategy_config.get("keep_groups", 10)

        # MemoryManager 单例
        self._memory_manager: SoulV5MemoryManager = SoulV5MemoryManager.get_instance()

        # Token 计数器（与 manager 共享模型）
        self._token_counter: TokenCounter = self._memory_manager.token_counter

        # 记忆工具实例（延迟初始化）
        self._memory_tools: Dict[str, Any] = {}

        # 记忆召回引擎（延迟初始化）
        self._recaller: Any = None

        # 本轮暂存
        self._pending_messages: List[Dict[str, Any]] = []
        self._current_group_id: Optional[str] = None
        self._messages: List[UnifiedMessage] = []

        _rlog.info(
            f"session_{session_id}",
            f"[SoulV5ContextHandler] 初始化 (keep_groups={self.keep_groups})",
        )

    # ------------------------------------------------------------------
    # 记忆工具实例化
    # ------------------------------------------------------------------

    def _ensure_memory_tools(self) -> None:
        """延迟实例化所有记忆工具"""
        if self._memory_tools:
            return

        import importlib

        for tool_name, class_name in _TOOL_REGISTRY.items():
            module_rel = _TOOL_MODULE_MAP[class_name]
            module = importlib.import_module(module_rel, package=__package__)
            tool_cls = getattr(module, class_name)

            tool_config = {
                "tool_type": "soulv5_memory",
                "tool_name": tool_name,
                "enabled": True,
            }
            self._memory_tools[tool_name] = tool_cls(tool_config, self._memory_manager)

        _rlog.info(
            f"session_{self.session_id}",
            f"[SoulV5ContextHandler] 已实例化 {len(self._memory_tools)} 个记忆工具",
        )

    # ------------------------------------------------------------------
    # BaseContextHandlerV3 生命周期
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: Dict[str, Any]) -> Dict[str, Any]:
        """对话开始前准备上下文

        1. 确保 MemoryManager DB 就绪
        2. 分配 group_id / 暂存 user 消息
        3. 加载偏好 → 注入系统提示
        4. 从 groups 加载历史（token-budgeted）
        5. 构建记忆 + 普通工具列表
        """
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")

        # 0. 等待该会话的上一轮压缩完成（auto-compress 是 fire-and-forget）
        await self._memory_manager.wait_compression_done(self.session_id)

        # 1. 延迟初始化 DB
        await self._memory_manager.ensure_db()

        # 1.5 补录磁盘上未入库的 group 文件（含 history 消息消化）
        try:
            all_history = self.history_manager.load_all()
            await self._memory_manager.ensure_session_groups_indexed(
                self.session_id, all_history or None,
            )
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV5ContextHandler] 补录 group 索引失败: {e}",
            )

        # 2. group_id
        if "group_id" not in user_msg:
            self._current_group_id = self._generate_group_id()
            user_msg["group_id"] = self._current_group_id
            await self._session_task_handler.log_info(
                f"[SoulV5ContextHandler] 分配 group_id: {self._current_group_id}"
            )

        # 暂存 user 消息
        self._pending_messages.append(user_msg)

        # 3. 系统提示 + 偏好 + 记忆召回
        user_text = user_msg.get("content", "")
        system = await self.get_system_prompt(user_text=user_text)

        # 4. 历史消息
        self._messages = await self._build_unified_messages()

        # 5. 工具列表
        tool_list = await self._build_tool_list()

        # 6. 将当前 user 消息追加到 _messages
        raw_parts = user_msg.get("content_parts")
        if raw_parts:
            content_parts = [_deserialize_content_part(p) for p in raw_parts]
            user_unified = UnifiedMessage(role="user", content_parts=content_parts)
        else:
            user_unified = UnifiedMessage(role="user", text=user_msg.get("content", ""))
        self._messages.append(user_unified)

        await self._session_task_handler.log_info(
            f"[SoulV5ContextHandler] 上下文就绪: {len(self._messages)} msgs, "
            f"{len(tool_list) if tool_list else 0} tools"
        )

        return {
            "system": system,
            "messages": self._messages,
            "tool_list": tool_list,
        }

    async def handle_after_llm_call(
        self,
        text_reply: str = "",
        tool_calls: Optional[List[ToolCall]] = None,
        reasoning: Optional[ReasoningArtifact] = None,
        produced_by_provider: Optional[str] = None,
        produced_by_model: Optional[str] = None,
    ) -> List[UnifiedMessage]:
        """LLM 调用后，暂存 assistant 消息"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")

        assistant_msg: Dict[str, Any] = {
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

        assistant_unified = UnifiedMessage(
            role="assistant", text=text_reply, tool_calls=tool_calls,
            reasoning=reasoning,
            produced_by_provider=produced_by_provider,
            produced_by_model=produced_by_model,
        )
        self._messages.append(assistant_unified)

        await self._session_task_handler.log_info(
            f"[SoulV5ContextHandler] 暂存 assistant "
            f"(tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"msgs={len(self._messages)})"
        )
        return self._messages

    async def handle_memory_tool_calls(
        self,
        tool_calls: List[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> Optional[Dict[str, Any]]:
        """拦截 soulv5_memory_* 工具调用并执行

        非记忆工具透传给 Agent 后续执行。
        """
        self._ensure_memory_tools()

        memory_calls: List[ToolCall] = []
        non_memory_calls: List[ToolCall] = []

        for tc in tool_calls:
            if tc.name.startswith(_SOULV5_TOOL_PREFIX) and tc.name in self._memory_tools:
                memory_calls.append(tc)
            else:
                non_memory_calls.append(tc)

        if not memory_calls:
            return {
                "memory_tool_results": [],
                "non_memory_calls": tool_calls,
            }

        await loop_task_handler.log_info(
            f"[SoulV5ContextHandler] 拦截 {len(memory_calls)} 个记忆工具调用"
        )

        results: List[ToolCallResult] = []
        for tc in memory_calls:
            tool = self._memory_tools[tc.name]
            try:
                # 解析参数
                if isinstance(tc.arguments, str):
                    kwargs = json.loads(tc.arguments)
                elif isinstance(tc.arguments, dict):
                    kwargs = tc.arguments
                else:
                    kwargs = {}

                # 注入上下文信息
                kwargs["_session_id"] = self.session_id
                kwargs["_workspace_path"] = str(self.workspace_path)
                kwargs["_group_id"] = self._current_group_id

                result = await tool.execute(**kwargs)
                content = json.dumps(
                    {"status": result.status.value, "output": result.output, "error": result.error},
                    ensure_ascii=False,
                )
            except Exception as e:
                content = json.dumps(
                    {"status": "failed", "error": str(e)},
                    ensure_ascii=False,
                )
                await loop_task_handler.log_error(
                    f"[SoulV5ContextHandler] 记忆工具 {tc.name} 异常: {e}\n{traceback.format_exc()}"
                )

            results.append(ToolCallResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=content,
            ))

        return {
            "memory_tool_results": results,
            "non_memory_calls": non_memory_calls,
        }

    async def handle_after_tool_calls(
        self,
        tool_results: List[ToolCallResult],
        last_call_prompt: Optional[str] = None,
        loop_task_handler: Optional[SessionTaskHandlerV2] = None,
    ) -> List[UnifiedMessage]:
        """工具调用后，暂存工具结果"""
        if not loop_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")

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

        if self._current_group_id:
            user_msg["group_id"] = self._current_group_id

        self._pending_messages.append(user_msg)

        user_unified = UnifiedMessage(
            role="user",
            tool_results=tool_results,
            text=last_call_prompt or None,
        )
        self._messages.append(user_unified)

        await loop_task_handler.log_info(
            f"[SoulV5ContextHandler] 暂存工具结果 "
            f"(results={len(tool_results)}, msgs={len(self._messages)}, last_call={bool(last_call_prompt)})"
        )
        return self._messages

    async def handle_after_loop(
        self,
        final_message: Dict[str, Any],
    ) -> None:
        """对话结束收尾

        1. 暂存最终消息（去重）
        2. 批量写盘到 history
        3. 保存 group 到 MemoryManager
        4. 检查并触发自动压缩
        """
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")

        # 附加 group_id
        if self._current_group_id and "group_id" not in final_message:
            final_message["group_id"] = self._current_group_id

        # 去重追加 final_message
        if final_message.get("content"):
            should_append = True
            if self._pending_messages:
                last = self._pending_messages[-1]
                if (last.get("role") == "assistant"
                        and last.get("content") == final_message.get("content")):
                    should_append = False
            if should_append:
                self._pending_messages.append(final_message)

        # 批量写盘 history
        if self._pending_messages:
            ok = self.history_manager.append_messages(self._pending_messages)
            await self._session_task_handler.log_info(
                f"[SoulV5ContextHandler] 批量写盘 {len(self._pending_messages)} 条 (ok={ok})"
            )

        # 保存 group 到 memory manager
        if self._current_group_id and self._pending_messages:
            try:
                await self._memory_manager.save_group(
                    session_id=self.session_id,
                    group_id=self._current_group_id,
                    messages=self._pending_messages,
                )
            except Exception as e:
                await self._session_task_handler.log_error(
                    f"[SoulV5ContextHandler] 保存 group 失败: {e}\n{traceback.format_exc()}"
                )

        # 检查自动压缩
        await self._maybe_auto_compress()

        # 清空
        self._pending_messages.clear()
        self._current_group_id = None
        self._messages.clear()

    async def handle_interruption(self) -> None:
        """中断处理"""
        self._pending_messages.clear()
        self._messages.clear()
        self._current_group_id = None
        if self._session_task_handler:
            await self._session_task_handler.log_info(
                "[SoulV5ContextHandler] 中断，已清空暂存",
            )
        else:
            _rlog.info(
                f"session_{self.session_id}",
                "[SoulV5ContextHandler] 中断，已清空暂存",
            )

    async def handle_compress(self) -> Optional[str]:
        """手动压缩入口（/compress 命令）

        将未索引的 groups 提炼为 cases，积累足够 case 后提炼 experience。
        """
        if not self._session_task_handler:
            return "❌ SessionTaskHandler 未设置，无法压缩"

        from ..task_manager import TaskType

        if self._memory_manager.is_session_compressing(self.session_id):
            return "⏳ 压缩已在后台运行，请稍后再试"

        try:
            compress_handler = await self._session_task_handler.create_sibling_task(
                task_type=TaskType.MEMORY_COMPRESS,
                name="manual-compress",
                description=f"Session {self.session_id} 手动压缩",
            )
            await compress_handler.start()
            await compress_handler.log_info(
                "[SoulV5ContextHandler] /compress 命令开始执行",
            )
            result = await self._memory_manager.compact_session(
                session_id=self.session_id,
                session_task_handler=compress_handler,
            )
            parts = [
                f"cases_created={result['cases_created']}",
                f"experiences_created={result['experiences_created']}",
                f"groups_indexed={result['groups_indexed']}",
            ]
            if result.get("errors"):
                await compress_handler.fail(error="[ERROR] " + "\n ".join(result["errors"]))
                parts.append(f"errors={len(result['errors'])}")
            await compress_handler.complete()
            return f"✅ 压缩完成: {', '.join(parts)}"
        except Exception as e:
            await compress_handler.log_error(
                f"[SoulV5ContextHandler] /compress 失败: {e}\n{traceback.format_exc()}"
            )
            await compress_handler.fail(error=f"[ERROR] 压缩失败: {e}")
            return f"❌ 压缩失败: {e}"

    async def force_compress(self, use_llm: bool = False) -> str:
        """/compress 命令兼容入口（command_handler 检查此方法）"""
        result = await self.handle_compress()
        return result or "完成"

    async def rebuild_memory_index(self) -> str:
        """重建 SQLite 索引（/rebuild_memory_index 命令入口）"""
        try:
            stats = await self._memory_manager.rebuild_index()
            return (
                f"✅ 索引重建完成: "
                f"nodes={stats['nodes']}, edges={stats['edges']}, content={stats['content']}"
            )
        except Exception as e:
            return f"❌ 索引重建失败: {e}"

    async def handle_spawn_context_snapshot(self) -> Dict[str, Any]:
        """为子代理提供上下文快照"""
        return await BaseContextHandlerV3.handle_spawn_context_snapshot(self)

    # ------------------------------------------------------------------
    # 系统提示
    # ------------------------------------------------------------------

    async def get_system_prompt(self, user_text: str = "") -> Optional[str]:
        """构建系统提示：基础模板 + 偏好注入 + 记忆召回 + 记忆工具说明"""
        from .system_prompts.simple_v2 import SIMPLE_V2_SYSTEM_PROMPT
        from ..config import get_session_config

        prompt = SIMPLE_V2_SYSTEM_PROMPT.format(
            workspace_root=self.workspace_path.absolute().as_posix(),
            project_root=get_session_config(self.session_id).get(
                "session_metadata", {}
            ).get("project_root", "."),
        )

        # 注入偏好
        try:
            prefs = await self._memory_manager.get_preferences(self.workspace_path)
            pref_sections = []
            if prefs.get("user"):
                pref_sections.append(f"## 用户偏好\n{prefs['user']}")
            if prefs.get("project"):
                pref_sections.append(f"## 项目偏好\n{prefs['project']}")
            if pref_sections:
                prompt += "\n\n# 偏好设定\n\n" + "\n\n".join(pref_sections)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV5ContextHandler] 加载偏好失败: {e}",
            )

        # 注入自动召回的相关记忆
        if user_text:
            try:
                if self._recaller is None:
                    from .soulv5_memory_recaller import SoulV5MemoryRecaller
                    self._recaller = SoulV5MemoryRecaller(self._memory_manager)
                recalled = await self._recaller.recall(
                    user_text, self._session_task_handler
                )
                if recalled:
                    prompt += "\n\n" + recalled
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[SoulV5ContextHandler] 记忆召回失败: {e}",
                )

        # 附加记忆工具说明
        prompt += _MEMORY_TOOLS_INSTRUCTION

        # 附加技能列表
        skills_info = await self._get_skills_info()
        if skills_info:
            prompt += "\n\n---\n\n" + skills_info

        # 当前时间
        prompt += f"\n\n# 当前时间\n{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"

        return prompt

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _build_unified_messages(self) -> List[UnifiedMessage]:
        """从 MD group 文件加载历史消息（token-budgeted）"""
        try:
            budget = self._memory_manager.context_window_cap // 2  # 一半给历史
            raw_messages = await self._memory_manager.load_recent_groups(
                session_id=self.session_id,
                token_budget=budget,
            )
            if raw_messages:
                return self.records_to_unified_messages(raw_messages)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV5ContextHandler] 从 groups 加载历史失败: {e}，回退到 history_manager",
            )

        # 回退：从 history 文件加载（与 SimpleContextV2 一致）
        msgs = self.history_manager.load_all()
        if not msgs:
            return []
        # 保留最近 keep_groups 组
        recent = msgs[-(self.keep_groups * 4):]  # 粗略估算
        return self.records_to_unified_messages(recent)

    async def _build_tool_list(self) -> Optional[List[ToolDefinition]]:
        """合并普通工具 + 记忆工具"""
        tool_defs: List[ToolDefinition] = []

        # 普通工具
        try:
            from ..tool import get_tool_manager
            tool_manager = get_tool_manager()
            for tool_name in tool_manager.list_loaded_tools():
                tool = tool_manager.get_tool(tool_name)
                if tool and tool.is_enabled():
                    desc = tool.get_description()
                    tool_defs.append(tool_description_to_definition(desc))
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV5ContextHandler] 构建普通工具列表失败: {e}",
            )

        # 记忆工具
        self._ensure_memory_tools()
        for tool_name, tool in self._memory_tools.items():
            if tool.is_enabled():
                desc = tool.get_description()
                tool_defs.append(tool_description_to_definition(desc))

        return tool_defs if tool_defs else None

    async def _maybe_auto_compress(self) -> None:
        """检查是否需要自动压缩，满足条件时 fire-and-forget 触发"""
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")
        # 若该会话的压缩锁已被持有，说明已有压缩任务在运行，跳过
        if self._memory_manager.is_session_compressing(self.session_id):
            return
        try:
            threshold = self._memory_manager.auto_group_threshold
            unindexed = await self._memory_manager.count_unindexed_groups(
                self.session_id
            )
            if unindexed >= threshold:
                await self._session_task_handler.log_info(
                    f"[SoulV5ContextHandler] 自动压缩触发 "
                    f"(unindexed={unindexed} >= threshold={threshold})"
                )
                asyncio.create_task(self._auto_compress())
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV5ContextHandler] 自动压缩检查失败: {e}",
            )

    async def _auto_compress(self) -> None:
        """后台自动压缩任务（fire-and-forget）"""
        try:
            from ..task_manager import TaskType

            if not self._session_task_handler:
                return

            compress_handler = await self._session_task_handler.create_sibling_task(
                task_type=TaskType.MEMORY_COMPRESS,
                name="auto-compress",
                description=f"Session {self.session_id} 自动压缩",
            )
            await compress_handler.start()
            result = await self._memory_manager.compact_session(
                session_id=self.session_id,
                session_task_handler=compress_handler,
            )
            await compress_handler.log_info(
                f"[SoulV5ContextHandler] 自动压缩完成: {result}",
            )
            await compress_handler.complete()
        except Exception as e:
            await compress_handler.log_error(
                f"[SoulV5ContextHandler] 自动压缩失败: {e}\n{traceback.format_exc()}",
            )
            await compress_handler.fail(error=f"[ERROR] 自动压缩失败: {e}")

    async def _get_skills_info(self) -> str:
        """获取可用技能列表"""
        try:
            from ..skill import get_skill_manager
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
                lines.append(f"- **描述**: {skill.description or '（无描述）'}")
                lines.append("")
            lines.append("> 使用技能前，请先读取对应路径下的 `SKILL.md` 获取完整指引。")
            return "\n".join(lines)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV5ContextHandler] 获取技能信息失败: {e}",
            )
            return ""

    def _generate_group_id(self) -> str:
        """生成 group_id: g_{date}_{HHMMSS}_{uuid6}"""
        now = datetime.now()
        return f"g_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------
    # BaseContextHandler 抽象方法
    # ------------------------------------------------------------------

    async def get_recent_messages(self, count: int) -> List[Dict[str, Any]]:
        """获取最近 count 条消息"""
        msgs = self.history_manager.load_all()
        if not msgs:
            return []
        return msgs[-count:] if len(msgs) > count else msgs

    def get_info(self) -> Dict[str, Any]:
        """返回处理器配置信息"""
        return {
            "type": "soul_v5",
            "session_id": self.session_id,
            "workspace_path": str(self.workspace_path),
            "keep_groups": self.keep_groups,
            "md_root": str(self._memory_manager.md_root),
            "db_path": str(self._memory_manager.db_path),
        }


# ---------------------------------------------------------------------------
# 记忆工具使用说明（注入系统提示）
# ---------------------------------------------------------------------------

_MEMORY_TOOLS_INSTRUCTION = """

# 多层记忆系统

你拥有一个基于文件树 + 索引的多层记忆系统，由以下工具组成：

## 工具列表

| 工具名 | 用途 | 只读 |
|--------|------|------|
| `soulv5_memory_query` | 全文搜索 case / experience / topic | ✅ |
| `soulv5_memory_read` | 按 ID 读取记忆文件全文 | ✅ |
| `soulv5_memory_save_case` | 创建 case（问题+方案+结果） | ❌ |
| `soulv5_memory_save_experience` | 创建 experience（操作指南） | ❌ |
| `soulv5_memory_update` | 更新已有 case 或 experience | ❌ |
| `soulv5_memory_browse_topics` | 浏览话题索引 | ✅ |
| `soulv5_memory_update_preferences` | 更新偏好 | ❌ |
| `soulv5_memory_deprecate` | 标记记忆为过期 | ❌ |

## 记忆层次

1. **Group** — 单轮对话原始消息（自动保存，不可编辑）
2. **Case** — 从 groups 中提炼的问题+方案+结果
3. **Experience** — 从 cases 中总结的操作指南
4. **Topic** — 话题索引，自动关联 case 和 experience
5. **Preference** — 用户/项目偏好（每次对话自动注入系统提示）

## 使用指南

- 遇到相似问题时 **先搜索** (`soulv5_memory_query`)
- 解决非常规问题后 **主动创建 case**
- 同一话题积累 3+ case 后 **提炼 experience**
- 用户明确提出偏好时 **更新 preferences**
- 发现过时信息时 **标记 deprecate**
"""
