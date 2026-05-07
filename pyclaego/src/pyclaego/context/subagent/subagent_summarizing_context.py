"""SubAgentSummarizingContextHandler — Token 感知 + LLM 驱动自动压缩子 Agent 上下文处理器

在 BaseSubAgentContextHandler 基础上增加：

  1. 工具结果落盘（SubAgentSummarizingArtifactStore）
     - >= warn_tokens 的工具输出落盘；> truncate_tokens 时 inline 截断至截断阈值

  2. token-offset 工具结果读取工具（tool_result_read）
     - 暴露给子 Agent LLM，支持 start_token / token_limit 分页读取

  3. LLM 主动摘要驱逐工具（tool_result_summarize_and_evict）
     - 暴露给子 Agent LLM，子 Agent 主动调用则计入正常轮次

  4. 自动冻结循环（Frozen Loop）—— 对子 Agent 完全透明
     - 触发条件：工具结果 > warn_threshold 或总 token 占用 > pressure_threshold
     - 在 handle_after_tool_calls 内部同步执行，子 Agent 轮次计数不变
     - 每次冻结迭代：一次 LLM 调用 + 若干工具调用（evict/read），无内层循环
     - 冻结 LLM 使用子 Agent 系统提示词 + 压缩任务附加说明
     - 冻结过程中产生的消息写入 _pending_messages（持久化审计记录）

  5. 上下文页脚注入
     - 在 handle_before_loop 中追加 "[Context: X% (xK/yK tokens) | Round N/M]"
     - 通过 **kwargs 接收 round_count / max_rounds（代理传入，缺省不显示 Round 部分）

窗口大小：取 config["window_size"] 与 LLM provider max_context_tokens 的较小值。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...llm import ToolCall, ToolCallResult, UnifiedMessage
from ...logging import get_running_log
from ...security_executor import SecurityHandler
from ...task_manager import SessionTaskHandlerV2, TaskType
from ..token_counter import TokenCounter
from .base_subagent_context import BaseSubAgentContextHandler
from .subagent_summarizing_artifact_store import SubAgentSummarizingArtifactStore
from .subagent_summarizing_evict_tool import (
    TOOL_NAME as _EVICT_TOOL_NAME,
)
from .subagent_summarizing_evict_tool import (
    SubAgentSummarizingEvictTool,
)
from .subagent_summarizing_tool_result_read_tool import (
    TOOL_NAME as _READ_TOOL_NAME,
)
from .subagent_summarizing_tool_result_read_tool import (
    SubAgentSummarizingToolResultReadTool,
)

_rlog = get_running_log()

_COMPRESS_SYSTEM_SUFFIX = "\n\n---\n[COMPRESSION TASK]\n"


class SubAgentSummarizingContextHandler(BaseSubAgentContextHandler):
    """Token 感知 + LLM 驱动自动压缩子 Agent 上下文处理器

    子 Agent 完全不感知冻结循环——它只是从 handle_after_tool_calls 得到
    已经压缩好的 _messages，然后继续正常执行。
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

        # Strategy config: self.config == context_subagents dict
        cfg: dict[str, Any] = self.config.get("summarizing_subagent", {})
        self._config_window_size: int = int(cfg.get("window_size", 128_000))
        self._compress_llm_id: str = str(cfg.get("compress_llm", "kimi_code"))
        self._pressure_threshold: float = float(cfg.get("context_pressure_threshold", 0.80))
        self._warn_tokens: int = int(cfg.get("tool_result_warn_tokens", 5_000))
        self._max_frozen_iterations: int = int(cfg.get("max_frozen_iterations", 3))
        truncate_tokens: int = int(cfg.get("tool_result_truncate_tokens", 10_000))

        self._artifact_store = SubAgentSummarizingArtifactStore(
            workspace_path=workspace_path,
            warn_tokens=self._warn_tokens,
            truncate_tokens=truncate_tokens,
        )

        self._round_count = 0
        self._max_rounds = 0
        self._read_tool = SubAgentSummarizingToolResultReadTool(store=self._artifact_store)
        self._evict_tool = SubAgentSummarizingEvictTool(store=self._artifact_store, ctx=self)
        self._token_counter = TokenCounter()
        self._security_handler = SecurityHandler.get_instance()

        _rlog.info(
            f"session_{session_id}",
            f"[SubAgentSummarizingContextHandler] 初始化 "
            f"(compress_llm={self._compress_llm_id}, "
            f"window={self._config_window_size}, "
            f"pressure_threshold={self._pressure_threshold}, "
            f"warn_tokens={self._warn_tokens}, "
            f"truncate_tokens={truncate_tokens}, "
            f"max_frozen={self._max_frozen_iterations})",
        )

    # ------------------------------------------------------------------
    # Window size: min(config, LLM provider max_context_tokens)
    # ------------------------------------------------------------------

    def _effective_window_size(self) -> int:
        try:
            from ...config import get_config
            llm_window = (
                get_config()
                .get("llm", {})
                .get("providers", {})
                .get(self._compress_llm_id, {})
                .get("max_context_tokens", self._config_window_size)
            )
            if not isinstance(llm_window, int) or llm_window <= 0:
                llm_window = self._config_window_size
        except Exception:
            llm_window = self._config_window_size
        return min(self._config_window_size, llm_window)

    # ------------------------------------------------------------------
    # Token counting helpers
    # ------------------------------------------------------------------

    def _count_context_tokens(self) -> int:
        """统计 system + _messages 的总 token 数（含工具调用/结果内容）"""
        total = self._token_counter.count_tokens(self._subagent_system)
        for msg in self._messages:
            if msg.text:
                total += self._token_counter.count_tokens(msg.text)
            for tr in (msg.tool_results or []):
                total += self._token_counter.count_tokens(tr.content or "")
            for tc in (msg.tool_calls or []):
                if tc.arguments:
                    args_str = (
                        json.dumps(tc.arguments)
                        if isinstance(tc.arguments, dict)
                        else str(tc.arguments)
                    )
                    total += self._token_counter.count_tokens(args_str)
        return total

    def _is_evicted_in_messages(self, tool_call_id: str) -> bool:
        """检查指定 tool_call_id 在 _messages 中是否已被 LLM 摘要替换"""
        for msg in self._messages:
            for tr in (getattr(msg, "tool_results", None) or []):
                if getattr(tr, "tool_call_id", "") == tool_call_id:
                    content = getattr(tr, "content", "") or ""
                    return content.startswith("[SUMMARY")
        return False

    def _get_evictable_artifacts(self) -> dict[str, int]:
        """返回已落盘但尚未被 LLM 摘要的工具结果 {tool_call_id: original_tokens}"""
        result: dict[str, int] = {}
        for artifact in self._artifact_store.list_artifacts(self.session_id):
            if not self._is_evicted_in_messages(artifact.tool_call_id):
                result[artifact.tool_call_id] = artifact.total_tokens
        return result

    # ------------------------------------------------------------------
    # handle_before_loop: inject context footer
    # ------------------------------------------------------------------

    async def handle_before_loop(
        self,
        user_msg: dict[str, Any],
        max_rounds: int,
        **kwargs,
    ) -> dict[str, Any]:
        """准备上下文并注入 token 占用页脚"""
        self._round_count += 1
        self._max_rounds = max_rounds

        result = await super().handle_before_loop(user_msg, max_rounds=max_rounds, **kwargs)

        return result

    # ------------------------------------------------------------------
    # handle_memory_tool_calls: intercept read + evict (voluntary subagent calls)
    # ------------------------------------------------------------------

    async def handle_memory_tool_calls(
        self,
        tool_calls: list[ToolCall],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any] | None:
        """拦截 tool_result_read 和 tool_result_summarize_and_evict，其余透传"""
        internal_calls: list[ToolCall] = []
        remaining: list[ToolCall] = []

        for tc in tool_calls:
            if tc.name in (_READ_TOOL_NAME, _EVICT_TOOL_NAME):
                internal_calls.append(tc)
            else:
                remaining.append(tc)

        if not internal_calls:
            return await super().handle_memory_tool_calls(tool_calls, loop_task_handler)

        internal_results: list[ToolCallResult] = []
        for tc in internal_calls:
            sub = await loop_task_handler.create_subtask(
                task_type=TaskType.MEMORY_EVICT,
                name=f"SubAgentSummarizing: {tc.name}",
                metadata={"tool": tc.name, "tool_call_id": tc.id},
            )
            await sub.start()
            try:
                tc_kwargs: dict[str, Any] = {}
                if isinstance(tc.arguments, str):
                    tc_kwargs = json.loads(tc.arguments) if tc.arguments.strip() else {}
                elif isinstance(tc.arguments, dict):
                    tc_kwargs = tc.arguments

                if tc.name == _READ_TOOL_NAME:
                    exec_result = await self._read_tool.execute(**tc_kwargs)
                else:
                    exec_result = await self._evict_tool.execute(**tc_kwargs)

                content = json.dumps(
                    {
                        "status": exec_result.status,
                        "output": exec_result.output,
                        "error": exec_result.error,
                    },
                    ensure_ascii=False,
                )
                await sub.complete({"status": exec_result.status})
            except Exception as e:
                content = json.dumps({"status": "failed", "error": str(e)}, ensure_ascii=False)
                try:
                    await sub.fail(error=str(e))
                except Exception:
                    pass

            internal_results.append(
                ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=content,
                )
            )

        # Delegate remaining to parent (pass-through)
        parent = await super().handle_memory_tool_calls(remaining, loop_task_handler)
        parent_mem = parent.get("memory_tool_results", []) if parent else []
        parent_non = parent.get("non_memory_calls", remaining) if parent else remaining

        return {
            "memory_tool_results": internal_results + parent_mem,
            "non_memory_calls": parent_non,
        }

    # ------------------------------------------------------------------
    # handle_after_tool_calls: spill → append → pressure check → frozen loop
    # ------------------------------------------------------------------

    async def handle_after_tool_calls(
        self,
        tool_results: list[ToolCallResult],
        loop_task_handler: SessionTaskHandlerV2,
        last_call_prompt: str | None = None,
    ) -> list[UnifiedMessage]:
        """落盘超大结果，追加消息，检测上下文压力，必要时触发冻结循环"""
        self._round_count += 1

        if not tool_results:
            return self._messages

        # Step 1: Spill oversized results; skip recovery tools to avoid re-spill loop
        processed: list[ToolCallResult] = []
        oversized_ids: list[str] = []

        for tr in tool_results:
            content = tr.content or ""
            if tr.tool_name in (_READ_TOOL_NAME, _EVICT_TOOL_NAME):
                processed.append(tr)
                continue
            if self._artifact_store.should_spill(content):
                artifact = await self._artifact_store.spill(
                    session_id=self.session_id,
                    tool_call_id=tr.tool_call_id,
                    tool_name=tr.tool_name,
                    content=content,
                )
                inline_content = self._artifact_store.render_inline(artifact, content)
                processed.append(
                    ToolCallResult(
                        tool_call_id=tr.tool_call_id,
                        tool_name=tr.tool_name,
                        content=inline_content,
                        content_parts=tr.content_parts,
                    )
                )
                oversized_ids.append(tr.tool_call_id)
            else:
                processed.append(tr)

        # Step 2: Append tool results to _messages via parent
        messages = await super().handle_after_tool_calls(processed, last_call_prompt)

        # Step 3: Update footer with token usage info (context + round count)
        window = self._effective_window_size()
        t_total = self._count_context_tokens()
        pct = int(t_total * 100 / window) if window > 0 else 0

        footer = (
            f"\n[Context: {pct}% ({t_total // 1000}K/{window // 1000}K tokens)"
            + (f" | Round {self._round_count}/{self._max_rounds}" if self._max_rounds > 0 else "")
            + "]"
        )

        # Step 4: Mutate last UnifiedMessage in _messages
        if messages:
            last_msg = messages[-1]
            last_msg.text = (last_msg.text or "") + footer

        # Step 5: Mutate last _pending_messages entry (same user_msg dict is already appended)
        if self._pending_messages:
            pm = self._pending_messages[-1]
            if isinstance(pm, dict) and pm.get("role") == "user":
                pm["content"] = (pm.get("content") or "") + footer

        # Step 6: Check pressure and trigger frozen loop when needed
        if loop_task_handler:
            window = self._effective_window_size()
            t_total = self._count_context_tokens()
            context_pressure = (t_total / window) > self._pressure_threshold if window > 0 else False
            needs_freeze = bool(oversized_ids) or context_pressure

            if needs_freeze:
                await loop_task_handler.log_info(
                    f"[SubAgentSummarizing] 触发冻结循环: "
                    f"oversized={oversized_ids}, "
                    f"context={t_total}/{window} "
                    f"({t_total * 100 // window if window else 0}%)"
                )
                await self._run_frozen_loop(
                    target_ids=oversized_ids,
                    window_size=window,
                    loop_task_handler=loop_task_handler,
                )
                return self._messages

        return messages

    # ------------------------------------------------------------------
    # handle_after_loop: report artifacts + super
    # ------------------------------------------------------------------

    async def handle_after_loop(self, final_message: dict[str, Any]) -> None:
        artifacts = self._artifact_store.list_artifacts(self.session_id)
        if artifacts and self._session_task_handler:
            try:
                sub = await self._session_task_handler.create_subtask(
                    task_type=TaskType.MEMORY_EVICT,
                    name="SubAgentSummarizing artifact report",
                    metadata={"artifact_count": len(artifacts)},
                )
                await sub.start()
                await sub.complete(
                    {
                        "artifacts": [
                            {
                                "tool_call_id": a.tool_call_id,
                                "tool_name": a.tool_name,
                                "total_tokens": a.total_tokens,
                                "path": str(a.path),
                            }
                            for a in artifacts
                        ]
                    }
                )  # type: ignore[attr-defined]
            except Exception as e:
                if self._session_task_handler:
                    await self._session_task_handler.log_error(
                        f"[SubAgentSummarizingContextHandler] artifact report 失败: {e}"
                    )

        await super().handle_after_loop(final_message)

    # ------------------------------------------------------------------
    # _build_tool_list: parent tools + read + evict
    # ------------------------------------------------------------------

    def _build_tool_list(self):
        parent_tools = super()._build_tool_list() or []
        return list(parent_tools) + [
            self._read_tool.get_tool_definition(),
            self._evict_tool.get_tool_definition(),
        ]

    # ------------------------------------------------------------------
    # _run_frozen_loop: internal compression (opaque to subagent)
    # ------------------------------------------------------------------

    async def _run_frozen_loop(
        self,
        target_ids: list[str],
        window_size: int,
        loop_task_handler: SessionTaskHandlerV2,
    ) -> None:
        """冻结循环：内部调用 LLM 压缩上下文，子 Agent 完全不感知。

        每次迭代 = 1次 LLM 调用 + 若干工具调用（evict/read），无内层循环。

        退出条件：
          - LLM 不调用任何工具（认为压缩已完成）
          - LLM 调用失败
          - 达到 max_frozen_iterations
        """
        frozen_task = await loop_task_handler.create_subtask(
            task_type=TaskType.MEMORY_EVICT,
            name="SubAgentSummarizing frozen compress",
            metadata={
                "target_ids": target_ids,
                "window_size": window_size,
            },
        )
        await frozen_task.start()

        try:
            for freeze_iter in range(1, self._max_frozen_iterations + 1):
                t_total = self._count_context_tokens()
                pct = t_total / window_size if window_size > 0 else 0.0
                evictable = self._get_evictable_artifacts()

                await frozen_task.log_info(
                    f"[Frozen] iter={freeze_iter}/{self._max_frozen_iterations} "
                    f"context={t_total}/{window_size}({pct:.1%}) "
                    f"evictable={list(evictable.keys())}"
                )

                compress_system = self._build_compress_system(
                    t_total=t_total,
                    window_size=window_size,
                    pct=pct,
                    target_ids=target_ids,
                    evictable=evictable,
                )

                iter_task = await frozen_task.create_subtask(
                    task_type=TaskType.MEMORY_EVICT,
                    name=f"Frozen iter {freeze_iter}",
                    metadata={"freeze_iter": freeze_iter},
                )
                await iter_task.start()

                # One LLM call per frozen iteration
                llm_result = await self._security_handler.request_subagent_llm_call(
                    session_id=self.session_id,
                    subagent_id=f"compress_{self.session_id}",
                    llm_id=self._compress_llm_id,
                    system=compress_system,
                    messages=self._messages,
                    task_handler=iter_task,
                    tool_list=[
                        self._read_tool.get_tool_definition(),
                        self._evict_tool.get_tool_definition(),
                    ],
                )

                if not llm_result["success"]:
                    await iter_task.log_error(
                        f"[Frozen] iter={freeze_iter} LLM 调用失败: {llm_result.get('error')}"
                    )
                    await iter_task.complete()
                    break

                v2_resp = llm_result["v2_response"]
                text_reply: str = v2_resp.text or ""
                tool_calls = v2_resp.tool_calls

                # Append internal assistant message (persisted in _pending_messages)
                now = datetime.now(timezone.utc).isoformat()
                internal_asst_dict: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_reply,
                    "timestamp": now,
                    "type": "assistant",
                    "_frozen_internal": True,
                }
                if tool_calls:
                    internal_asst_dict["tool_calls"] = [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in tool_calls
                    ]
                # 保留 thinking/reasoning 产物，避免丢失导致下轮 API 400
                if v2_resp.reasoning:
                    internal_asst_dict["reasoning"] = v2_resp.reasoning.to_dict()
                if v2_resp.produced_by_provider:
                    internal_asst_dict["produced_by_provider"] = v2_resp.produced_by_provider
                if v2_resp.produced_by_model:
                    internal_asst_dict["produced_by_model"] = v2_resp.produced_by_model
                self._pending_messages.append(internal_asst_dict)
                self._messages.append(
                    UnifiedMessage(
                        role="assistant",
                        text=text_reply,
                        tool_calls=tool_calls,
                        reasoning=v2_resp.reasoning,
                        produced_by_provider=v2_resp.produced_by_provider,
                        produced_by_model=v2_resp.produced_by_model,
                    )
                )

                if not tool_calls:
                    await iter_task.log_info(
                        f"[Frozen] iter={freeze_iter} LLM 未调用工具，结束冻结"
                    )
                    await iter_task.complete()
                    break

                # Execute tool calls (evict/read only) — one pass, no inner loop
                tool_result_dicts: list[dict[str, Any]] = []
                tool_result_unified: list[ToolCallResult] = []

                for tc in tool_calls:
                    tc_kwargs: dict[str, Any] = {}
                    try:
                        if isinstance(tc.arguments, str):
                            tc_kwargs = json.loads(tc.arguments) if tc.arguments.strip() else {}
                        elif isinstance(tc.arguments, dict):
                            tc_kwargs = tc.arguments

                        if tc.name == _EVICT_TOOL_NAME:
                            exec_result = await self._evict_tool.execute(**tc_kwargs)
                        elif tc.name == _READ_TOOL_NAME:
                            exec_result = await self._read_tool.execute(**tc_kwargs)
                        else:
                            # Disallowed tool in frozen loop — return error without executing
                            from .subagent_summarizing_evict_tool import EvictToolExecuteResult
                            exec_result = EvictToolExecuteResult(
                                status="failed",
                                output="",
                                error=(
                                    f"Tool '{tc.name}' is not allowed in the frozen "
                                    "compression loop. Only tool_result_read and "
                                    "tool_result_summarize_and_evict are permitted."
                                ),
                            )

                        content = json.dumps(
                            {
                                "status": exec_result.status,
                                "output": exec_result.output,
                                "error": exec_result.error,
                            },
                            ensure_ascii=False,
                        )
                    except Exception as e:
                        content = json.dumps(
                            {"status": "failed", "error": str(e)}, ensure_ascii=False
                        )

                    tool_result_dicts.append(
                        {"tool_call_id": tc.id, "tool_name": tc.name, "content": content}
                    )
                    tool_result_unified.append(
                        ToolCallResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=content,
                        )
                    )

                # Append internal tool result message
                now2 = datetime.now(timezone.utc).isoformat()
                self._pending_messages.append(
                    {
                        "role": "user",
                        "tool_results": tool_result_dicts,
                        "timestamp": now2,
                        "type": "tool_result",
                        "_frozen_internal": True,
                    }
                )
                self._messages.append(
                    UnifiedMessage(
                        role="user",
                        tool_results=tool_result_unified,
                    )
                )

                await iter_task.complete({"tool_calls_executed": len(tool_calls)})

            else:
                # Loop exhausted without break
                await frozen_task.log_warning(
                    f"[Frozen] 达到最大冻结迭代次数 ({self._max_frozen_iterations})，强制退出"
                )

            await frozen_task.complete()

        except Exception as e:
            await frozen_task.log_error(f"[Frozen] 异常: {e}")
            try:
                await frozen_task.fail(error=str(e))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # _build_compress_system: compression LLM system prompt
    # ------------------------------------------------------------------

    def _build_compress_system(
        self,
        t_total: int,
        window_size: int,
        pct: float,
        target_ids: list[str],
        evictable: dict[str, int],
    ) -> str:
        """构建冻结循环 LLM 的系统提示词"""
        lines = [
            self._subagent_system,
            _COMPRESS_SYSTEM_SUFFIX,
            f"Context is at {pct:.0%} ({t_total}/{window_size} tokens).",
            "Your task is to free up context space by summarizing large tool results.",
            "Call tool_result_summarize_and_evict to replace tool results with concise summaries.",
            "Use tool_result_read first if you need to review the content before writing a summary.",
            "When you are done, respond without calling any tools.",
            "",
        ]

        if target_ids:
            lines.append("Priority targets (flagged as too large):")
            for tid in target_ids:
                tokens = evictable.get(tid)
                if tokens is not None:
                    lines.append(f"  - tool_call_id={tid!r}  ({tokens} tokens original)")
                else:
                    lines.append(f"  - tool_call_id={tid!r}  (already evicted or unavailable)")

        remaining = {tid: tok for tid, tok in evictable.items() if tid not in target_ids}
        if remaining:
            lines.append("")
            lines.append("Other evictable artifacts (optional):")
            for tid, tok in remaining.items():
                lines.append(f"  - tool_call_id={tid!r}  ({tok} tokens original)")

        if not evictable:
            lines.append(
                "No artifacts are available for eviction. "
                "Context is full due to accumulated conversation history."
            )

        return "\n".join(lines)
