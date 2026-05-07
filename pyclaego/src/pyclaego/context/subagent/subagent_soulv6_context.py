"""SubAgentSoulV6ContextHandler — 子 Agent V6 上下文处理器基类

在 BaseSubAgentContextHandler 基础上增加：

  1. 工具结果落盘（SubAgentSoulV6ArtifactStore）
     - 超过 token 阈值的工具输出写到磁盘，消息列表保留占位符
  2. 过时驱逐（SubAgentSoulV6StaleEvictor）
     - 每轮工具调用后，驱逐旧轮次中过大的工具结果（SUMMARIZE / DROP）
  3. 预算分配（SubAgentSoulV6BudgetAllocator）
     - 显式记录各 tenant 的 token 预算，供未来扩展使用
  4. tool_result_read 工具注入
     - 向 LLM 暴露 ``subagent_soulv6_tool_result_read`` 工具，供按需读取落盘内容
  5. 技能注入（SkillManager）
     - 在 handle_before_loop 中将可用技能列表追加到 system prompt

子类只需：
  - 覆盖 ALLOWED_TOOLS（过滤工具列表）
  - 在 __init__ 中传入合适的 system prompt 和配置
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...llm import ToolCall, ToolCallResult, UnifiedMessage
from ...logging import get_running_log
from ...security_executor.record_store import RecordStore
from ...task_manager import ArtifactReporter, SessionTaskHandlerV2, TaskType, safe_attach
from .base_subagent_context import BaseSubAgentContextHandler
from .subagent_soulv6_artifact_store import SubAgentSoulV6ArtifactStore
from .subagent_soulv6_budget_allocator import (
    SubAgentSoulV6BudgetAllocator,
    SubAgentSoulV6BudgetPlan,
)
from .subagent_soulv6_stale_evictor import (
    SubAgentSoulV6EvictAction,
    SubAgentSoulV6StaleEvictor,
)
from .subagent_soulv6_tool_result_read_tool import (
    TOOL_NAME as _READ_TOOL_NAME,
)
from .subagent_soulv6_tool_result_read_tool import (
    SubAgentSoulV6ToolResultReadTool,
)

_rlog = get_running_log()


class SubAgentSoulV6ContextHandler(BaseSubAgentContextHandler):
    """子 Agent V6 上下文处理器基类

    子类可直接使用或进一步覆盖 ALLOWED_TOOLS / _build_tool_list。
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        memory_mode: str = "empty",
        initial_messages: list[UnifiedMessage] | None = None,
        initial_system: str | None = None,
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            workspace_path=workspace_path,
            config=config,
            memory_mode=memory_mode,
            initial_messages=initial_messages,
            initial_system=initial_system,
            session_task_handler=session_task_handler,
        )

        # V6 策略配置（从 context 切片的 soul_v6_subagent 键读取，缺省 OK）
        v6_cfg: dict[str, Any] = self.config.get("soul_v6_subagent", {})
        store_cfg: dict[str, Any] = v6_cfg.get("artifact_store", {})
        evict_cfg: dict[str, Any] = v6_cfg.get("stale_evictor", {})
        budget_cfg: dict[str, Any] = v6_cfg.get("budget", {})

        # 工具结果存储
        self._artifact_store = SubAgentSoulV6ArtifactStore(
            workspace_path=workspace_path,
            spill_token_threshold=int(store_cfg.get("spill_token_threshold", 2_000)),
            head_chars=int(store_cfg.get("head_chars", 1_000)),
            tail_chars=int(store_cfg.get("tail_chars", 500)),
        )

        # 过时驱逐器
        self._stale_evictor = SubAgentSoulV6StaleEvictor(
            store=self._artifact_store,
            keep_recent_turns=int(evict_cfg.get("keep_recent_turns", 2)),
            summarize_tokens_threshold=int(
                evict_cfg.get("summarize_tokens_threshold", 1_000)
            ),
            drop_tokens_threshold=int(evict_cfg.get("drop_tokens_threshold", 4_000)),
            head_chars_summary=int(evict_cfg.get("head_chars_summary", 400)),
        )

        # 预算分配器
        self._budget_allocator = SubAgentSoulV6BudgetAllocator.from_config(budget_cfg)
        self._last_budget_plan: SubAgentSoulV6BudgetPlan | None = None

        # 内联工具（上下文绑定，不注册到全局 ToolManager）
        self._read_tool = SubAgentSoulV6ToolResultReadTool(store=self._artifact_store)

        # 轮次计数（用于驱逐器的 round_index）
        self._round_count: int = 0

        _rlog.info(
            f"session_{session_id}",
            f"[SubAgentSoulV6ContextHandler] 初始化 "
            f"(spill_threshold={self._artifact_store.spill_token_threshold}, "
            f"keep_recent_turns={self._stale_evictor.keep_recent_turns}, "
            f"workspace={workspace_path})",
        )

    # ------------------------------------------------------------------
    # handle_before_loop：追加技能信息到 system prompt
    # ------------------------------------------------------------------

    async def handle_before_loop(self, user_msg: dict[str, Any]) -> dict[str, Any]:
        result = await super().handle_before_loop(user_msg)

        skills_info = await self._get_skills_info()
        if skills_info:
            result["system"] = (result.get("system") or "") + "\n\n---\n\n" + skills_info

        return result

    # ------------------------------------------------------------------
    # handle_after_tool_calls：落盘 → super → 驱逐
    # ------------------------------------------------------------------

    async def handle_after_tool_calls(  # type: ignore[override]
        self,
        tool_results: list[ToolCallResult],
        last_call_prompt: str | None = None,
        loop_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> list[UnifiedMessage]:
        if not tool_results:
            return self._messages

        effective_handler = loop_task_handler or self._session_task_handler

        # Step 1: 落盘超大工具结果
        spilled_count = 0
        processed: list[ToolCallResult] = []

        for tr in tool_results:
            content = tr.content or ""
            # 恢复工具（recovery tool）的结果不能再次落盘——否则会形成递归溢出陷阱：
            # LLM 调用 tool_result_read 来读取溢出内容 → 结果再次溢出 → 再次调用 → 无限循环。
            if tr.tool_name == _READ_TOOL_NAME:
                processed.append(tr)
                continue
            if self._artifact_store.should_spill(content):
                artifact = await self._artifact_store.spill(
                    session_id=self.session_id,
                    tool_call_id=tr.tool_call_id,
                    tool_name=tr.tool_name,
                    content=content,
                )
                placeholder = self._artifact_store.render_placeholder(artifact)
                processed.append(
                    ToolCallResult(
                        tool_call_id=tr.tool_call_id,
                        tool_name=tr.tool_name,
                        content=placeholder,
                    )
                )
                spilled_count += 1
            else:
                processed.append(tr)

        if spilled_count and effective_handler:
            await effective_handler.log_info(
                f"[SubAgentSoulV6ContextHandler] handle_after_tool_calls "
                f"spilled={spilled_count}/{len(tool_results)}"
            )

        # Step 2: 调用父类追加消息
        # BaseSubAgentContextHandler.handle_after_tool_calls 不接受 loop_task_handler
        messages = await super().handle_after_tool_calls(
            processed, last_call_prompt=last_call_prompt
        )

        # Step 3: 驱逐过时工具结果
        self._round_count += 1
        if effective_handler:
            await self._evict_stale(effective_handler)

        return messages

    # ------------------------------------------------------------------
    # handle_memory_tool_calls：拦截 subagent_soulv6_tool_result_read
    # ------------------------------------------------------------------

    async def handle_memory_tool_calls(  # type: ignore[override]
        self,
        tool_calls: list[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any] | None:
        read_calls: list[ToolCall] = []
        remaining: list[ToolCall] = []

        for tc in tool_calls:
            if tc.name == _READ_TOOL_NAME:
                read_calls.append(tc)
            else:
                remaining.append(tc)

        read_results: list[ToolCallResult] = []
        for tc in read_calls:
            sub = await loop_task_handler.create_subtask(
                task_type=TaskType.MEMORY_READ,
                name=f"SubAgentSoulV6: {_READ_TOOL_NAME}",
                metadata={"tool": tc.name, "tool_call_id": tc.id},
            )
            await sub.start()
            reporter = ArtifactReporter.for_task(sub._task_id)
            kwargs: dict[str, Any] = {}
            start_ts = datetime.now(timezone.utc).isoformat()
            try:
                if isinstance(tc.arguments, str):
                    kwargs = json.loads(tc.arguments) if tc.arguments.strip() else {}
                elif isinstance(tc.arguments, dict):
                    kwargs = tc.arguments

                safe_attach(reporter, "tool_args", tc.name, kwargs)
                start_ts = datetime.now(timezone.utc).isoformat()

                result = await self._read_tool.execute(**kwargs)
                content = json.dumps(
                    {
                        "status": result.status,
                        "output": result.output,
                        "error": result.error,
                    },
                    ensure_ascii=False,
                )
                end_ts = datetime.now(timezone.utc).isoformat()
                safe_attach(reporter, "tool_result", tc.name, content, success=True)
                RecordStore.get_instance().write_subagent_memory_tool_call(
                    session_id=self.session_id,
                    subagent_id=sub._task_id,
                    tool_name=tc.name,
                    tool_args=kwargs,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    success=True,
                    output=result.output or "",
                    error=result.error or "",
                )
                await sub.complete({"status": result.status})
            except Exception as e:
                end_ts = datetime.now(timezone.utc).isoformat()
                await loop_task_handler.log_error(
                    f"[SubAgentSoulV6ContextHandler] {_READ_TOOL_NAME} 异常: {e}"
                )
                content = json.dumps(
                    {"status": "failed", "error": str(e)},
                    ensure_ascii=False,
                )
                safe_attach(reporter, "error_trace", str(e), name=f"error[{tc.name}]")
                RecordStore.get_instance().write_subagent_memory_tool_call(
                    session_id=self.session_id,
                    subagent_id=sub._task_id,
                    tool_name=tc.name,
                    tool_args=kwargs,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    success=False,
                    error=str(e),
                )
                try:
                    await sub.fail(error=str(e))
                except Exception:
                    pass

            read_results.append(
                ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=content,
                )
            )

        # 委托父类处理剩余（父类透传全部为 non_memory_calls）
        parent = await super().handle_memory_tool_calls(remaining, loop_task_handler)
        parent_mem = parent.get("memory_tool_results", []) if parent else []
        parent_non = parent.get("non_memory_calls", remaining) if parent else remaining

        return {
            "memory_tool_results": read_results + parent_mem,
            "non_memory_calls": parent_non,
        }

    # ------------------------------------------------------------------
    # _build_tool_list：父类工具 + read tool
    # ------------------------------------------------------------------

    def _build_tool_list(self):  # type: ignore[override]
        parent_tools = super()._build_tool_list() or []
        read_def = self._read_tool.get_tool_definition()
        return list(parent_tools) + [read_def]

    # ------------------------------------------------------------------
    # handle_after_loop：上报 artifact 列表，然后 super
    # ------------------------------------------------------------------

    async def handle_after_loop(self, final_message: dict[str, Any]) -> None:
        artifacts = self._artifact_store.list_artifacts(self.session_id)
        if artifacts and self._session_task_handler:
            try:
                sub = await self._session_task_handler.create_subtask(
                    task_type=TaskType.MEMORY_EVICT,
                    name="SubAgentSoulV6 artifact report",
                    metadata={
                        "session_id": self.session_id,
                        "artifact_count": len(artifacts),
                    },
                )
                await sub.start()
                await sub.complete(
                    {
                        "artifacts": [
                            {
                                "tool_call_id": a.tool_call_id,
                                "tool_name": a.tool_name,
                                "total_tokens": a.total_tokens,
                                "total_chars": a.total_chars,
                                "path": str(a.path),
                            }
                            for a in artifacts
                        ]
                    }
                )
            except Exception as e:
                if self._session_task_handler:
                    await self._session_task_handler.log_error(
                        f"[SubAgentSoulV6ContextHandler] artifact report 失败: {e}"
                    )

        await super().handle_after_loop(final_message)

    # ------------------------------------------------------------------
    # _evict_stale：内部驱逐执行
    # ------------------------------------------------------------------

    async def _evict_stale(self, loop_task_handler: SessionTaskHandlerV2) -> None:
        if not self._messages:
            return

        sub = await loop_task_handler.create_subtask(
            task_type=TaskType.MEMORY_EVICT,
            name="SubAgentSoulV6 stale tool_result evict",
            metadata={"round": self._round_count},
        )
        await sub.start()

        decisions = self._stale_evictor.decide(
            messages=self._messages,
            current_round_idx=self._round_count,
        )

        drops = 0
        summaries = 0
        evicted_tokens = 0

        for dec in decisions:
            if dec.action == SubAgentSoulV6EvictAction.KEEP:
                continue

            msg = self._messages[dec.message_index]
            tool_results = getattr(msg, "tool_results", None) or []
            for tr in tool_results:
                if tr.tool_call_id != dec.tool_call_id:
                    continue
                old_tokens = self._artifact_store.count_tokens(tr.content or "")
                new_text = dec.replacement_text or ""
                tr.content = new_text
                tr.content_parts = None  # type: ignore[attr-defined]
                evicted_tokens += max(0, old_tokens - self._artifact_store.count_tokens(new_text))
                if dec.action == SubAgentSoulV6EvictAction.DROP:
                    drops += 1
                elif dec.action == SubAgentSoulV6EvictAction.SUMMARIZE:
                    summaries += 1

        if drops or summaries:
            await sub.log_info(
                f"[SubAgentSoulV6ContextHandler] evict: "
                f"drops={drops} summaries={summaries} saved≈{evicted_tokens} tokens"
            )

        await sub.complete(
            {
                "drops": drops,
                "summaries": summaries,
                "saved_tokens": evicted_tokens,
            }
        )

    # ------------------------------------------------------------------
    # 技能信息（移植自 SoulV5ContextHandler._get_skills_info）
    # ------------------------------------------------------------------

    async def _get_skills_info(self) -> str:
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
                lines.append(f"- **描述**: {skill.description or '（无描述）'}")
                lines.append("")
            lines.append("> 使用技能前，请先读取对应路径下的 `SKILL.md` 获取完整指引。")
            return "\n".join(lines)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SubAgentSoulV6ContextHandler] 获取技能信息失败: {e}",
            )
            return ""
