"""SoulV6ContextHandler — SoulV5ContextHandler 的 V6 兄弟版

Phase 1: 独立 memory manager、独立配置、独立磁盘路径。
Phase 2: 工具结果落盘 + 过时驱逐 + `tool_result_read` 工具注入。

后续阶段将在本文件继续扩展：
- Phase 3: turn brief synthesizer
- Phase 4: 分层 recaller
- Phase 5: write review & forgetting
- Phase 6: 观测

V6 不修改 V5：通过继承与方法覆盖实现扩展。
"""

from __future__ import annotations

from pathlib import Path
import traceback
from typing import Any, Dict, List, Optional, Set

from .soulv5_context_handler import SoulV5ContextHandler
from .soulv6_memory_manager import SoulV6MemoryManager
from .soulv6_budget_allocator import SoulV6BudgetAllocator, SoulV6BudgetPlan
from .soulv6_tool_result_store import SoulV6ToolResultStore
from .soulv6_stale_evictor import SoulV6StaleEvictor, SoulV6EvictAction
from .soulv6_turn_brief import SoulV6TurnBriefSynthesizer, SoulV6TurnBrief
from .soulv6_open_loops import SoulV6OpenLoopsStore
from .soulv6_entity_cards import SoulV6EntityCardStore
from .soulv6_write_review import (
    SoulV6MemoryWriteReview, SoulV6WriteAction, SoulV6WriteReviewResult,
)
from .soulv6_metrics import SoulV6MetricsCollector
from ..llm import UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition, tool_description_to_definition
from ..task_manager import SessionTaskHandlerV2, TaskType
from ..logging import get_running_log

_rlog = get_running_log()

_SOULV6_TOOL_RESULT_READ = "soulv6_tool_result_read"


class SoulV6ContextHandler(SoulV5ContextHandler):
    """SoulV6 上下文处理器（Phase 1-2）"""

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: Dict[str, Any],
        session_task_handler: Optional[SessionTaskHandlerV2] = None,
    ) -> None:
        from .base_context import BaseContextHandlerV3

        BaseContextHandlerV3.__init__(
            self, session_id, workspace_path, config, session_task_handler
        )

        strategy_config: Dict[str, Any] = config.get("soul_v6", {})
        self.keep_groups: int = strategy_config.get("keep_groups", 10)

        # V6 独立 MemoryManager
        self._memory_manager: SoulV6MemoryManager = SoulV6MemoryManager.get_instance()
        self._memory_manager.ensure_directory_structure()
        self._token_counter = self._memory_manager.token_counter

        # 延迟实例化
        self._memory_tools: Dict[str, Any] = {}
        self._recaller: Any = None

        # 本轮暂存
        self._pending_messages: List[Dict[str, Any]] = []
        self._current_group_id: Optional[str] = None
        self._messages: List[UnifiedMessage] = []

        # V6: 预算分配器
        self._budget_allocator: SoulV6BudgetAllocator = (
            SoulV6BudgetAllocator.from_config(self._memory_manager.budget_config)
        )
        self._last_budget_plan: Optional[SoulV6BudgetPlan] = None

        # V6: 工具结果存储
        store_cfg: Dict[str, Any] = strategy_config.get("tool_result_store", {})
        self._tool_result_store: SoulV6ToolResultStore = SoulV6ToolResultStore(
            memory_manager=self._memory_manager,
            spill_token_threshold=store_cfg.get("spill_token_threshold", 2_000),
            head_chars=store_cfg.get("head_chars", 1_000),
            tail_chars=store_cfg.get("tail_chars", 500),
            max_read_chars=store_cfg.get("max_read_chars", 12_000),
        )

        # V6: 过时驱逐器
        evict_cfg: Dict[str, Any] = strategy_config.get("stale_evictor", {})
        self._stale_evictor: SoulV6StaleEvictor = SoulV6StaleEvictor(
            store=self._tool_result_store,
            keep_recent_turns=evict_cfg.get("keep_recent_turns", 2),
            summarize_tokens_threshold=evict_cfg.get("summarize_tokens_threshold", 1_000),
            drop_tokens_threshold=evict_cfg.get("drop_tokens_threshold", 4_000),
            head_chars_summary=evict_cfg.get("head_chars_summary", 400),
        )

        # 用户 /pin 过的 tool_call_id（Phase 5 持久化到 .memory/soul_v6/pinned/{session_id}.json）
        self._pinned_tool_call_ids: Set[str] = set()
        self._pinned_path = (
            self._memory_manager.md_root / "pinned" / f"{self.session_id}.json"
        )
        self._load_pinned_from_disk()

        # V6: Phase 3 - turn brief & open loops
        brief_cfg: Dict[str, Any] = strategy_config.get("turn_brief", {})
        self._brief_synthesizer: SoulV6TurnBriefSynthesizer = (
            SoulV6TurnBriefSynthesizer.get_instance()
        )
        self._brief_wait_timeout: float = float(brief_cfg.get("wait_timeout_s", 0.5))
        self._verbatim_recent_groups: int = int(brief_cfg.get("verbatim_recent_groups", 3))
        self._briefs_max_older_groups: int = int(brief_cfg.get("max_older_groups", 30))
        self._brief_min_chars_for_llm: int = int(brief_cfg.get("min_chars_for_llm", 200))

        self._open_loops_store: SoulV6OpenLoopsStore = SoulV6OpenLoopsStore(
            memory_manager=self._memory_manager
        )
        self._open_loops_token_budget: int = int(
            strategy_config.get("open_loops", {}).get("token_budget", 600)
        )

        # V6: Phase 5 - entity cards & write review
        ec_cfg: Dict[str, Any] = strategy_config.get("entity_cards", {})
        self._entity_cards: SoulV6EntityCardStore = SoulV6EntityCardStore(
            memory_manager=self._memory_manager
        )
        self._entity_cards_token_budget: int = int(ec_cfg.get("token_budget", 800))
        self._entity_cards_top_k: int = int(ec_cfg.get("top_k", 8))

        wr_cfg: Dict[str, Any] = strategy_config.get("write_review", {})
        self._write_review: SoulV6MemoryWriteReview = SoulV6MemoryWriteReview(
            memory_manager=self._memory_manager,
            block_threshold=wr_cfg.get("block_threshold", 0.85),
            link_threshold=wr_cfg.get("link_threshold", 0.5),
            max_candidates=wr_cfg.get("max_candidates", 8),
        )
        self._write_review_enabled: bool = bool(wr_cfg.get("enabled", True))

        # V6: Phase 6 - observability
        self._metrics: SoulV6MetricsCollector = SoulV6MetricsCollector()

        _rlog.info(
            f"session_{session_id}",
            f"[SoulV6ContextHandler] 初始化 "
            f"(keep_groups={self.keep_groups}, "
            f"md_root={self._memory_manager.md_root}, "
            f"spill_threshold={self._tool_result_store.spill_token_threshold})",
        )

    # ------------------------------------------------------------------
    # 预算分配
    # ------------------------------------------------------------------

    def allocate_budget(self, total_window: Optional[int] = None) -> SoulV6BudgetPlan:
        window = total_window or self._memory_manager.context_window_cap
        plan = self._budget_allocator.allocate(window)
        self._last_budget_plan = plan
        return plan

    # ------------------------------------------------------------------
    # Phase 2.1: 工具列表加入 tool_result_read
    # ------------------------------------------------------------------

    def _ensure_memory_tools(self) -> None:  # type: ignore[override]
        """在 V5 记忆工具基础上追加 V6 专属工具（保持单例注册，避免重复）。"""
        super()._ensure_memory_tools()
        if _SOULV6_TOOL_RESULT_READ not in self._memory_tools:
            from .memory_tools.soulv6_tool_result_read_tool import SoulV6ToolResultReadTool
            self._memory_tools[_SOULV6_TOOL_RESULT_READ] = SoulV6ToolResultReadTool(
                tool_config={
                    "tool_type": "soulv6_memory",
                    "tool_name": _SOULV6_TOOL_RESULT_READ,
                    "enabled": True,
                },
                store=self._tool_result_store,
            )

    async def _build_tool_list(self) -> Optional[List[ToolDefinition]]:  # type: ignore[override]
        # V5 的 _build_tool_list 会调用 _ensure_memory_tools 并迭代 self._memory_tools，
        # V6 覆写后 read tool 已经在 _memory_tools 中，无需再手动追加（否则会重复）。
        return await super()._build_tool_list()

    # ------------------------------------------------------------------
    # handle_memory_tool_calls: 拦截 V6 的 tool_result_read（以及未来 V6 tools）
    # ------------------------------------------------------------------

    async def handle_memory_tool_calls(  # type: ignore[override]
        self,
        tool_calls: List[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> Optional[Dict[str, Any]]:
        """优先拦截 V6 工具，剩余交给 V5 父类处理 V5 记忆工具"""
        import json

        v6_names = {_SOULV6_TOOL_RESULT_READ}
        save_tool_names = {"memory_save_case", "memory_save_experience"}

        v6_calls: List[ToolCall] = []
        review_blocked: List[ToolCallResult] = []
        remaining: List[ToolCall] = []

        for tc in tool_calls:
            if tc.name in v6_names and tc.name in self._memory_tools:
                v6_calls.append(tc)
                continue

            # Phase 5: 写前审查 —— 拦截 memory_save_*
            if self._write_review_enabled and tc.name in save_tool_names:
                review_result = await self._review_save_tool_call(tc, loop_task_handler)
                if review_result is not None:
                    if review_result["block"]:
                        review_blocked.append(review_result["tool_result"])
                        continue
                    if review_result["mutated_args"] is not None:
                        # 替换 arguments 后续走父类 V5 工具流程
                        try:
                            tc.arguments = dict(review_result["mutated_args"])
                        except Exception:
                            pass
                    remaining.append(tc)
                    continue

            remaining.append(tc)

        v6_results: List[ToolCallResult] = []
        for tc in v6_calls:
            tool = self._memory_tools[tc.name]
            try:
                if isinstance(tc.arguments, str):
                    kwargs = json.loads(tc.arguments)
                elif isinstance(tc.arguments, dict):
                    kwargs = tc.arguments
                else:
                    kwargs = {}
                kwargs["_session_id"] = self.session_id
                kwargs["_workspace_path"] = str(self.workspace_path)
                kwargs["_group_id"] = self._current_group_id

                result = await tool.execute(**kwargs)
                content = json.dumps(
                    {"status": result.status.value, "output": result.output, "error": result.error},
                    ensure_ascii=False,
                )
            except Exception as e:
                await loop_task_handler.log_error(
                    f"[SoulV6ContextHandler] V6 工具 {tc.name} 异常: {e}"
                )
                content = json.dumps(
                    {"status": "failed", "error": str(e)},
                    ensure_ascii=False,
                )
            v6_results.append(ToolCallResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=content,
            ))

        # 委托父类处理 V5 记忆工具与非记忆工具分流
        parent = await super().handle_memory_tool_calls(remaining, loop_task_handler)
        parent_mem = parent.get("memory_tool_results", []) if parent else []
        parent_non = parent.get("non_memory_calls", remaining) if parent else remaining

        return {
            "memory_tool_results": v6_results + review_blocked + parent_mem,
            "non_memory_calls": parent_non,
        }

    # ------------------------------------------------------------------
    # Phase 5: 写前审查实现
    # ------------------------------------------------------------------

    async def _review_save_tool_call(
        self, tc: ToolCall, loop_task_handler: SessionTaskHandlerV2
    ) -> Optional[Dict[str, Any]]:
        """对 memory_save_* 调用执行写前审查。

        Returns:
            None — 调用方按原 args 走 V5 流程；
            {"block": True, "tool_result": ToolCallResult} —— 直接返回阻塞响应；
            {"block": False, "mutated_args": dict|None} —— 放行，可能改写 args。
        """
        import json
        try:
            if isinstance(tc.arguments, str):
                args = json.loads(tc.arguments) if tc.arguments.strip() else {}
            elif isinstance(tc.arguments, dict):
                args = dict(tc.arguments)
            else:
                args = {}
        except Exception:
            return None

        title = str(args.get("title", "")).strip()
        content = str(args.get("content", "")).strip()
        topic = str(args.get("topic", "")).strip()
        tags = args.get("tags") or []
        override = bool(args.get("override_conflict", False))
        if not title or not content:
            return None

        doc_type_hint = "case" if tc.name == "memory_save_case" else "experience"
        review = await self._write_review.review_save(
            title=title, content=content, topic=topic,
            tags=tags, doc_type_hint=doc_type_hint,
            override_conflict=override,
        )

        try:
            sub = await loop_task_handler.create_subtask(
                task_type=TaskType.MEMORY_WRITE_REVIEW,
                name=f"SoulV6 write review ({tc.name})",
                metadata={
                    "tool": tc.name,
                    "action": review.action.value,
                    "n_conflicts": len(review.conflicts),
                    "title": title[:80],
                },
            )
            await sub.complete()
        except Exception:
            pass

        if review.action == SoulV6WriteAction.BLOCK_PENDING:
            # Phase 6 metrics
            self._metrics.incr("write_review_total")
            self._metrics.incr("write_review_blocked")
            payload = {
                "status": "failed",
                "output": review.to_tool_response(),
                "error": review.block_message,
            }
            return {
                "block": True,
                "tool_result": ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=json.dumps(payload, ensure_ascii=False),
                ),
            }

        mutated_args: Optional[Dict[str, Any]] = None
        if review.action == SoulV6WriteAction.ALLOW_WITH_LINK and review.annotated_content:
            mutated_args = dict(args)
            mutated_args["content"] = review.annotated_content
            self._metrics.incr("write_review_total")
            self._metrics.incr("write_review_linked")
        elif review.action == SoulV6WriteAction.ALLOW:
            self._metrics.incr("write_review_total")

        return {"block": False, "mutated_args": mutated_args}


    # ------------------------------------------------------------------
    # Phase 2.2: handle_after_tool_calls - 大工具结果落盘
    # ------------------------------------------------------------------

    async def handle_after_tool_calls(  # type: ignore[override]
        self,
        tool_results: List[ToolCallResult],
        last_call_prompt: Optional[str] = None,
        loop_task_handler: Optional[SessionTaskHandlerV2] = None,
    ) -> List[UnifiedMessage]:
        if not loop_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")
        if not tool_results:
            return self._messages

        group_id = self._current_group_id or "no_group"
        spilled_count = 0
        processed_results: List[ToolCallResult] = []

        for tr in tool_results:
            if tr.content_parts is not None:
                processed_results.append(tr)
                continue

            if self._tool_result_store.should_spill(tr.content or ""):
                artifact = await self._tool_result_store.spill(
                    session_id=self.session_id,
                    group_id=group_id,
                    tool_call_id=tr.tool_call_id,
                    tool_name=tr.tool_name,
                    content=tr.content or "",
                )
                placeholder = self._tool_result_store.render_placeholder(artifact)
                processed_results.append(
                    ToolCallResult(
                        tool_call_id=tr.tool_call_id,
                        tool_name=tr.tool_name,
                        content=placeholder,
                        content_parts=None,
                    )
                )
                spilled_count += 1
                # Phase 6: metrics
                try:
                    saved = self._tool_result_store.count_tokens(tr.content or "") - \
                            self._tool_result_store.count_tokens(placeholder)
                    self._metrics.incr("spill_count")
                    self._metrics.add_tokens("spill_saved_tokens", max(0, saved))
                except Exception:
                    pass
            else:
                processed_results.append(tr)

        if spilled_count:
            await loop_task_handler.log_info(
                f"[SoulV6ContextHandler] handle_after_tool_calls "
                f"spilled={spilled_count}/{len(tool_results)}"
            )

        return await super().handle_after_tool_calls(processed_results, last_call_prompt, loop_task_handler=loop_task_handler)

    # ------------------------------------------------------------------
    # Phase 2.3: handle_before_llm_call - 过时驱逐
    # ------------------------------------------------------------------

    async def handle_before_llm_call(  # type: ignore[override]
        self,
        messages: List[UnifiedMessage],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> List[UnifiedMessage]:
        if not messages:
            return messages
        if not loop_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")

        mem_evict_handler = await loop_task_handler.create_subtask(
            task_type=TaskType.MEMORY_EVICT,
            name="SoulV6 stale tool_result evict",
        )
        referenced = self._scan_referenced_tool_ids(messages)

        decisions = self._stale_evictor.decide(
            messages=messages,
            current_group_id=self._current_group_id,
            pinned_tool_call_ids=self._pinned_tool_call_ids,
            referenced_tool_call_ids=referenced,
        )
        if not decisions:
            await mem_evict_handler.complete()
            return messages

        evicted_tokens = 0
        drops = 0
        summaries = 0

        for dec in decisions:
            if dec.action == SoulV6EvictAction.KEEP:
                continue
            msg = messages[dec.message_index]
            tool_results = getattr(msg, "tool_results", None) or []
            for tr in tool_results:
                if tr.tool_call_id != dec.tool_call_id:
                    continue
                old_tokens = self._tool_result_store.count_tokens(tr.content or "")
                new_text = dec.replacement_text or ""
                new_tokens = self._tool_result_store.count_tokens(new_text)
                tr.content = new_text
                tr.content_parts = None
                evicted_tokens += max(0, old_tokens - new_tokens)
                if dec.action == SoulV6EvictAction.DROP:
                    drops += 1
                elif dec.action == SoulV6EvictAction.SUMMARIZE:
                    summaries += 1

        # Phase 6: metrics counters
        if drops or summaries:
            self._metrics.incr("evict_drops", drops)
            self._metrics.incr("evict_summaries", summaries)
            self._metrics.add_tokens("evict_saved_tokens", evicted_tokens)

        if drops or summaries:
            await mem_evict_handler.log_info(
                f"[SoulV6ContextHandler] handle_before_llm_call "
                f"evict: drops={drops} summaries={summaries} "
                f"saved≈{evicted_tokens} tokens"
            )

        # Phase 6: 审计每个 tenant 的实际 token 用量
        try:
            plan = self._last_budget_plan or self.allocate_budget()
            self._metrics.audit_message_layout(
                messages=messages,
                plan=plan,
                token_counter=self._memory_manager.token_counter,
                context_window_cap=self._memory_manager.context_window_cap,
            )
            await mem_evict_handler.complete({
                "drops": drops,
                "summaries": summaries,
                "saved_tokens": evicted_tokens,
            })
        except Exception as e:
            await mem_evict_handler.log_error(
                f"[SoulV6ContextHandler] audit_message_layout 失败: {e}\n{traceback.format_exc()}",
            )
            await mem_evict_handler.fail(str(e))

        return messages

    # ------------------------------------------------------------------
    # 引用扫描
    # ------------------------------------------------------------------

    def _scan_referenced_tool_ids(
        self, messages: List[UnifiedMessage]
    ) -> Set[str]:
        referenced: Set[str] = set()
        known_ids: Set[str] = set()
        for m in messages:
            for tr in (getattr(m, "tool_results", None) or []):
                known_ids.add(tr.tool_call_id)
        if not known_ids:
            return referenced

        recent_assistant_texts: List[str] = []
        for m in reversed(messages):
            if getattr(m, "role", None) == "assistant":
                txt = getattr(m, "text", None) or ""
                if txt:
                    recent_assistant_texts.append(txt)
                if len(recent_assistant_texts) >= 3:
                    break

        for txt in recent_assistant_texts:
            for tcid in known_ids:
                if tcid in txt:
                    referenced.add(tcid)
        return referenced

    # ------------------------------------------------------------------
    # Pin API（为 Phase 5 的 /pin /unpin 命令预留）
    # ------------------------------------------------------------------

    def pin_tool_call(self, tool_call_id: str) -> None:
        self._pinned_tool_call_ids.add(tool_call_id)
        self._save_pinned_to_disk()

    def unpin_tool_call(self, tool_call_id: str) -> bool:
        if tool_call_id in self._pinned_tool_call_ids:
            self._pinned_tool_call_ids.remove(tool_call_id)
            self._save_pinned_to_disk()
            return True
        return False

    def _load_pinned_from_disk(self) -> None:
        try:
            if self._pinned_path.exists():
                import json as _json
                data = _json.loads(self._pinned_path.read_text(encoding="utf-8"))
                ids = data.get("pinned_tool_call_ids", []) if isinstance(data, dict) else []
                self._pinned_tool_call_ids = set(str(x) for x in ids)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] 加载 pinned 失败: {e}\n{traceback.format_exc()}",
            )

    def _save_pinned_to_disk(self) -> None:
        try:
            import json as _json
            self._pinned_path.parent.mkdir(parents=True, exist_ok=True)
            self._pinned_path.write_text(
                _json.dumps(
                    {"pinned_tool_call_ids": sorted(self._pinned_tool_call_ids)},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] 保存 pinned 失败: {e}",
            )

    # ------------------------------------------------------------------
    # Phase 4: 使用 V6 的 10-stage recaller 替代 V5 默认 recaller
    # ------------------------------------------------------------------

    async def get_system_prompt(self, user_text: str = "") -> Optional[str]:  # type: ignore[override]
        # 在 super 调用之前，注入 V6 recaller，让 V5 的延迟初始化逻辑使用 V6
        if self._recaller is None:
            from .soulv6_memory_recaller import SoulV6MemoryRecaller
            self._recaller = SoulV6MemoryRecaller(self._memory_manager)
        # Phase 6: 时延采集（包含 super 内的 recall 调用）
        self._metrics.stage_start("system_prompt_total")
        prompt = await super().get_system_prompt(user_text=user_text)
        self._metrics.stage_end("system_prompt_total")
        if not prompt:
            return prompt

        # Phase 5: 实体卡片注入
        try:
            ec_text = await self._entity_cards.render_for_context(
                token_budget=self._entity_cards_token_budget,
                top_k=self._entity_cards_top_k,
            )
            if ec_text:
                prompt = prompt + "\n\n" + ec_text
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] 注入 entity_cards 失败: {e}",
            )

        # Phase 3.2: 更早对话的摘要注入 system（避免产生连续 user 消息）
        try:
            _recent_files, older_group_ids = self._scan_group_files()
            briefs_text = await self._render_briefs_text(older_group_ids)
            if briefs_text:
                prompt = prompt + "\n\n" + briefs_text
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] 注入 briefs 失败: {e}",
            )

        # Phase 5: V6 工具/策略说明
        try:
            from .system_prompts.soulv6 import SOULV6_SYSTEM_PROMPT_SUFFIX
            prompt = prompt + SOULV6_SYSTEM_PROMPT_SUFFIX
        except Exception:
            pass

        return prompt

    # ------------------------------------------------------------------
    # Phase 5: 命令入口（供 CommandHandler 调用）
    # ------------------------------------------------------------------

    async def cmd_pin(self, tool_call_id: str) -> str:
        if not tool_call_id:
            return "❌ 用法: /pin <tool_call_id>"
        self.pin_tool_call(tool_call_id)
        return f"✅ 已固定 tool_call_id={tool_call_id}（不会被驱逐）"

    async def cmd_unpin(self, tool_call_id: str) -> str:
        if not tool_call_id:
            return "❌ 用法: /unpin <tool_call_id>"
        ok = self.unpin_tool_call(tool_call_id)
        return f"✅ 已解除固定 {tool_call_id}" if ok else f"• {tool_call_id} 当前未被固定"

    async def cmd_close_loop(self, query: str) -> str:
        """通过 id 或 topic 子串关闭 open loop"""
        q = (query or "").strip()
        if not q:
            return "❌ 用法: /close_loop <loop_id 或 topic 子串>"
        # 先按 id 精确匹配
        if q.startswith("ol-"):
            ok = await self._open_loops_store.close(self.session_id, q, reason="closed via /close_loop")
            return f"✅ 已关闭 {q}" if ok else f"• 未找到处于 open 状态的 loop: {q}"
        n = await self._open_loops_store.close_matching(
            self.session_id, q, reason="closed via /close_loop"
        )
        if n == 0:
            return f"• 没有匹配 '{q}' 的 open loop"
        return f"✅ 已关闭 {n} 条匹配 '{q}' 的 open loop"

    async def cmd_memories(self, args: List[str]) -> str:
        """概览记忆资产"""
        try:
            await self._memory_manager.ensure_db()
            db = self._memory_manager._db
            assert db is not None
            counts: Dict[str, int] = {}
            async with db.execute(
                "SELECT doc_type, COUNT(*) FROM nodes "
                "WHERE status = 'current' GROUP BY doc_type"
            ) as cur:
                async for row in cur:
                    counts[row[0]] = int(row[1])
        except Exception as e:
            return f"❌ 无法读取记忆索引: {e}"

        ec = await self._entity_cards.list_top_k(k=5, by="recent")
        ol_open = await self._open_loops_store.list_open(self.session_id)
        pinned = sorted(self._pinned_tool_call_ids)

        lines = [f"🧠 SoulV6 记忆概览 (session: {self.session_id})"]
        lines.append("  按 doc_type 统计:")
        for dt, n in sorted(counts.items()):
            lines.append(f"    {dt:<12} {n}")
        lines.append(f"  实体卡片: {len(await self._entity_cards.list_all())} 个")
        lines.append(f"  open_loops: {len(ol_open)} 个未闭合")
        lines.append(f"  pinned tool_call_ids: {len(pinned)}")
        if ec:
            lines.append("  最近实体:")
            for c in ec:
                lines.append(f"    - {c.display_name} ({c.kind}, ×{c.mention_count})")
        if ol_open:
            lines.append("  open loops:")
            for l in ol_open[:5]:
                lines.append(f"    - [{l.id}] {l.topic}")
        return "\n".join(lines)

    async def cmd_forget(self, md_path: str) -> str:
        """将一条记忆标记为 deprecated（status=archived）"""
        if not md_path:
            return "❌ 用法: /forget <md_path>"
        try:
            await self._memory_manager.ensure_db()
            db = self._memory_manager._db
            assert db is not None
            async with db.execute(
                "UPDATE nodes SET status='archived' WHERE md_path=?", (md_path,)
            ) as _cur:
                pass
            await db.commit()
        except Exception as e:
            return f"❌ /forget 失败: {e}"
        return f"✅ 已归档（archived）: {md_path}"

    async def cmd_why(self, query: str) -> str:
        """解释最近一次召回的来源（基于 V6 recaller 的 markdown）"""
        if self._recaller is None:
            return "• 本会话尚未触发自动召回"
        try:
            from .soulv6_memory_recaller import SoulV6MemoryRecaller
            assert isinstance(self._recaller, SoulV6MemoryRecaller)
        except Exception:
            return "• 当前 recaller 不是 SoulV6MemoryRecaller"
        if not query.strip():
            return "❌ 用法: /why <近似的用户问题>"
        text = await self._recaller.recall(query, self._session_task_handler)
        if not text:
            return "• 没有命中任何记忆"
        return "🔎 召回轨迹（重放 query）:\n\n" + text

    async def cmd_export_memory(self, target_dir: str) -> str:
        if not target_dir:
            return "❌ 用法: /export_memory <目标目录>"
        from pathlib import Path as _P
        import shutil as _sh
        src = self._memory_manager.md_root
        dst = _P(target_dir).expanduser().resolve()
        try:
            dst.mkdir(parents=True, exist_ok=True)
            target = dst / src.name
            if target.exists():
                _sh.rmtree(target)
            _sh.copytree(src, target)
        except Exception as e:
            return f"❌ 导出失败: {e}"
        return f"✅ 已导出 {src} → {target}"



    # ------------------------------------------------------------------
    # Phase 3.1: handle_before_loop —— 等待上一轮 brief，注入 open_loops
    # ------------------------------------------------------------------

    async def handle_before_loop(  # type: ignore[override]
        self, user_msg: Dict[str, Any]
    ) -> Dict[str, Any]:
        """V6 覆盖：在调用 V5 逻辑前等待上一轮 brief 完成；之后附加 open_loops 到 system。"""
        # 等待上一轮 brief（短超时，避免阻塞）
        try:
            done_n = await self._brief_synthesizer.wait_brief_done(
                self.session_id, timeout=self._brief_wait_timeout
            )
            if done_n and self._session_task_handler:
                await self._session_task_handler.log_info(
                    f"[SoulV6ContextHandler] wait_brief_done finished={done_n}"
                )
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] wait_brief_done 异常: {e}",
            )

        result = await super().handle_before_loop(user_msg)

        # 注入 open_loops 到 system prompt 尾部
        try:
            ol_text = await self._open_loops_store.render_for_context(
                self.session_id, token_budget=self._open_loops_token_budget
            )
            if ol_text:
                sys_prompt = result.get("system") or ""
                result["system"] = (sys_prompt + "\n\n" + ol_text).strip()
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] 注入 open_loops 失败: {e}",
            )

        return result

    # ------------------------------------------------------------------
    # Phase 3.2: 历史分层 —— 近 N 组逐字 + 更早的仅摘要
    # ------------------------------------------------------------------

    async def _build_unified_messages(self) -> List[UnifiedMessage]:  # type: ignore[override]
        """覆盖 V5：仅加载最近 N 组 verbatim 消息。

        更早的组以摘要形式注入 system prompt（见 ``get_system_prompt``），
        不再作为独立的 user 消息放进 history —— 否则会与 verbatim 层首条 user
        连续，违反 Anthropic user/assistant 交替约束。
        """
        try:
            recent_files, _older_ids = self._scan_group_files()
            if not recent_files:
                return []
            return await self._load_verbatim_groups(recent_files)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] V6 历史分层失败: {e}，回退 V5",
            )
            return await super()._build_unified_messages()

    def _scan_group_files(self) -> tuple[List[Path], List[str]]:
        """扫描 session 的 group 文件，按时间降序分成 verbatim 与 briefs 两层。

        Returns:
            (recent_files, older_group_ids)：recent_files 为降序的最近 N 个 md
            文件路径；older_group_ids 为更早的 group_id 列表（降序）。
        """
        from .soulv5_memory_manager import _DIR_GROUPS  # type: ignore
        mm = self._memory_manager
        group_dir = mm.md_root / _DIR_GROUPS / self.session_id
        if not group_dir.exists():
            return [], []
        md_files = sorted(group_dir.glob("*.md"), reverse=True)
        if not md_files:
            return [], []
        recent_n = max(0, self._verbatim_recent_groups)
        recent_files = md_files[:recent_n]
        older_files = md_files[recent_n : recent_n + self._briefs_max_older_groups]
        older_group_ids = [p.stem for p in older_files]
        return recent_files, older_group_ids

    async def _load_verbatim_groups(
        self, md_files: List[Path]
    ) -> List[UnifiedMessage]:
        """按给定 md 文件列表（降序）加载 verbatim 消息，内部仍按 token 预算截断。"""
        if not md_files:
            return []
        import json
        mm = self._memory_manager
        plan = self._last_budget_plan or self.allocate_budget()
        token_budget = plan.get("history_verbatim") or (mm.context_window_cap // 4)

        collected: List[List[Dict[str, Any]]] = []
        total_tokens = 0
        for md_file in md_files:  # newest first
            try:
                content = md_file.read_text(encoding="utf-8")
                _, body = mm._parse_front_matter_str(content)
                messages = mm._extract_messages_from_body(body)
                if not messages:
                    continue
                mm._truncate_tool_contents(messages)
                msg_tokens = mm.token_counter.count_tokens(
                    json.dumps(messages, ensure_ascii=False)
                )
                if total_tokens + msg_tokens > token_budget:
                    break
                total_tokens += msg_tokens
                collected.append(messages)
            except Exception as e:
                _rlog.error(
                    f"session_{self.session_id}",
                    f"[SoulV6ContextHandler] verbatim 解析 {md_file} 失败: {e}",
                )
        collected.reverse()
        flat: List[Dict[str, Any]] = []
        for msgs in collected:
            flat.extend(msgs)
        return self.records_to_unified_messages(flat) if flat else []

    async def _render_briefs_text(
        self, older_group_ids: List[str]
    ) -> Optional[str]:
        """将更早的 group 渲染成摘要文本（供 system prompt 注入）。

        返回已按时间升序拼接的文本；若无可渲染内容，返回 ``None``。
        """
        if not older_group_ids:
            return None
        plan = self._last_budget_plan or self.allocate_budget()
        token_budget = plan.get("history_briefs") or 3_000
        tc = self._memory_manager.token_counter

        briefs = await self._brief_synthesizer.load_briefs_for_groups(
            self.session_id, older_group_ids
        )

        # 对于缺失 brief 的 group，现场生成 fallback（不走 LLM：摘取首末两条消息）
        rendered_blocks: List[str] = []
        fallback_needed: List[str] = [
            gid for gid in older_group_ids
            if not any(b.group_id == gid for b in briefs)
        ]
        if fallback_needed:
            fallbacks = await self._fallback_briefs(fallback_needed)
            briefs = briefs + fallbacks

        # 排序：older_group_ids 给的顺序是降序，我们渲染时转为升序
        brief_by_id = {b.group_id: b for b in briefs}
        ordered = [
            brief_by_id[gid] for gid in reversed(older_group_ids)
            if gid in brief_by_id
        ]

        total_tokens = 0
        for b in ordered:
            block = b.render_compact()
            block_tokens = tc.count_tokens(block)
            if total_tokens + block_tokens > token_budget:
                rendered_blocks.append(
                    f"... ({len(ordered) - len(rendered_blocks)} 更早的轮次已省略)"
                )
                break
            rendered_blocks.append(block)
            total_tokens += block_tokens

        if not rendered_blocks:
            return None

        return "【更早的对话摘要（按时间升序）】\n\n" + "\n\n".join(rendered_blocks)

    async def _fallback_briefs(
        self, group_ids: List[str]
    ) -> List[SoulV6TurnBrief]:
        """为缺失 brief 的 group 生成一个极简 fallback（不持久化）。"""
        from .soulv5_memory_manager import _DIR_GROUPS  # type: ignore
        mm = self._memory_manager
        out: List[SoulV6TurnBrief] = []
        for gid in group_ids:
            md_path = mm.md_root / _DIR_GROUPS / self.session_id / f"{gid}.md"
            if not md_path.exists():
                continue
            try:
                raw = md_path.read_text(encoding="utf-8")
                _, body = mm._parse_front_matter_str(raw)
                msgs = mm._extract_messages_from_body(body) or []
                if not msgs:
                    continue
                first_user = next(
                    (m.get("content", "") for m in msgs if m.get("role") == "user"), ""
                )
                last_asst = next(
                    (m.get("content", "") for m in reversed(msgs)
                     if m.get("role") == "assistant"), ""
                )
                summary = (str(first_user)[:80] + " → " + str(last_asst)[:80]).strip()
                out.append(SoulV6TurnBrief(
                    session_id=self.session_id,
                    group_id=gid,
                    created_at="",
                    summary=summary or "(empty)",
                    turn_tokens=0,
                ))
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------
    # Phase 3.3: handle_after_loop —— fire-and-forget brief 合成
    # ------------------------------------------------------------------

    async def handle_after_loop(  # type: ignore[override]
        self, final_message: Dict[str, Any]
    ) -> None:
        if not self._session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")
        # 快照 group_id & pending_messages —— 父类 handle_after_loop 会清空 pending
        group_id = self._current_group_id
        messages_snapshot: List[Dict[str, Any]] = list(self._pending_messages)
        if final_message and final_message not in messages_snapshot:
            # 父类会去重 append；快照也反映相同意图
            if not (messages_snapshot and messages_snapshot[-1].get("role") == "assistant"
                    and messages_snapshot[-1].get("content") == final_message.get("content")):
                messages_snapshot.append(dict(final_message))

        await super().handle_after_loop(final_message)

        if not group_id or not messages_snapshot:
            return

        # 估算是否值得走 LLM brief
        try:
            import json
            total_chars = sum(
                len(str(m.get("content") or "")) for m in messages_snapshot
            )
            if total_chars < self._brief_min_chars_for_llm:
                # 非常短：写一条 minimal brief 即可（仍由 synthesizer 处理 <40 tokens 分支）
                pass

            # fire-and-forget
            self._brief_synthesizer.synthesize_async(
                session_id=self.session_id,
                group_id=group_id,
                messages=messages_snapshot,
                session_task_handler=self._session_task_handler,
            )

        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] 启动 brief 合成失败: {e}",
            )

        # Phase 6: emit per-turn metrics summary, then reset
        try:
            snapshot = self._metrics.reset()
            if self._session_task_handler:
                sub = await self._session_task_handler.create_subtask(
                    task_type=TaskType.MEMORY_BUDGET,
                    name=f"SoulV6 turn metrics (group={group_id})",
                    metadata={"group_id": group_id, **snapshot.to_metadata()},
                )
                await sub.complete()
                await self._session_task_handler.log_info(snapshot.render_human())
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SoulV6ContextHandler] emit metrics 失败: {e}",
            )

