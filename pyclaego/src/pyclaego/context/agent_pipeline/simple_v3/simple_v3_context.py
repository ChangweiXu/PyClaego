"""SimpleV3ContextHandler — SimpleV3 上下文策略

继承 BasePipelineContextHandler，实现三区预算模型 + 单次 llm_mini 压缩 +
scratch 工作记忆 + 派生缓存状态文件的轻量上下文管理。

配置键：``context.simple_v3``

核心特性：
- 三区预算（scratch / summary / verbatim），五个比率控制触发线
- 每轮 before_llm_call 在超标时调 llm_mini 做单次 offload/evict 决策
- 每轮 after_loop fire-and-forget 更新 scratch 工作草稿
- 状态文件 (.simple_v3/state.json) 为派生缓存，可全量重建
"""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....context.token_counter import TokenCounter
from ....llm import (
    ReasoningArtifact,
    ToolCall,
    ToolCallResult,
    ToolDefinition,
    UnifiedMessage,
    tool_description_to_definition,
)
from ....logging import get_running_log
from ....task_manager import SessionTaskHandlerV2, TaskType
from ..context import BasePipelineContextHandler
from .simple_v3_offload_store import SimpleV3OffloadStore
from .simple_v3_state_manager import SimpleV3StateManager

_rlog = get_running_log()

# 模块级 TokenCounter（lazy init，默认 gpt-4 编码）
_token_counter: TokenCounter | None = None


def _get_token_counter() -> TokenCounter:
    global _token_counter
    if _token_counter is None:
        _token_counter = TokenCounter()
    return _token_counter


def _estimate_tokens(text: str) -> int:
    """使用 tiktoken 精确估算 token 数"""
    if not text:
        return 0
    return _get_token_counter().count_tokens(text)


def _estimate_message_tokens(msg: UnifiedMessage) -> int:
    """估算单条 UnifiedMessage 的 token 数"""
    tc = _get_token_counter()
    total = 0
    if msg.text:
        total += tc.count_tokens(msg.text)
    if msg.tool_results:
        for tr in msg.tool_results:
            total += tc.count_tokens(tr.content or "")
    if msg.tool_calls:
        for tcall in msg.tool_calls:
            total += tc.count_tokens(tcall.name or "")
            args_str = json.dumps(tcall.arguments, ensure_ascii=False) if tcall.arguments else ""
            total += tc.count_tokens(args_str)
    if msg.content_parts:
        for cp in msg.content_parts:
            if hasattr(cp, "text") and cp.text:
                total += tc.count_tokens(cp.text)
    return max(1, total)


def _estimate_tokens_all(messages: list[UnifiedMessage]) -> int:
    """估算消息列表的总 token 数"""
    return sum(_estimate_message_tokens(m) for m in messages)

def _estimate_dict_message_tokens(msg: dict[str, Any]) -> int:
    """估算单条原始 dict 消息的 token 数（计入 content + tool_calls + tool_results）"""
    tc = _get_token_counter()
    total = 0
    content = msg.get("content", "") or ""
    if content:
        total += tc.count_tokens(str(content))
    for tcall in msg.get("tool_calls") or []:
        total += tc.count_tokens(str(tcall.get("name", "")))
        args = tcall.get("arguments", "")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        total += tc.count_tokens(str(args))
    for tr in msg.get("tool_results") or []:
        total += tc.count_tokens(str(tr.get("content", "")))
    return max(1, total)

# ------------------------------------------------------------------
# llm_mini 压缩 system prompt
# ------------------------------------------------------------------

_COMPRESS_SYSTEM_PROMPT = """你是上下文压缩助手。你的任务是根据消息元数据表格，决定哪些消息可以卸载（offload）或移除（evict），以将上下文 token 数降低到目标值以下。

**卸载 (offload)**：将消息完整内容保存到 OffloadStore，当前上下文中替换为简短摘要。
**移除 (evict)**：直接丢弃该消息（仅适用于很早的问候语/无意义消息）。

**决策优先级**：
1. 优先 offload 大的工具结果（tokens > 2000 且 role 包含 tool_result）
2. 其次 offload 旧轮次的对话（较早的消息索引）
3. 最新用户问题（最后一条 user 消息）和最近 2 轮 assistant 回复不可 offload
4. 包含当前问题关键词的消息保留
5. preview 中已包含 `[内容已卸载:` 的消息不可再次 offload（已是占位符，再次卸载无意义）

**输出格式**：严格 JSON（不要 markdown 代码块）：
{"offload": [{"msg_idx": 4, "summary": "users 表完整 DDL，约 5000 tokens"}], "evict": [0, 1]}

只输出 JSON，不要任何其他文字。"""

_SCRATCH_SYSTEM_PROMPT = """你是对话草稿助手。根据本轮对话摘要和当前工作草稿，生成更新后的草稿。

**草稿规则**：
- 只写当前任务状态（进度、决策、涉及文件、待做事项），不写对话历史
- 已完成的任务用 [x] 标记，进行中的用 [ ] 标记
- 包含关键决策和涉及的文件路径
- 草稿不得超过 {max_tokens} tokens（约 {max_chars} 字符）

**当前草稿**：
{current_scratch}

**本轮对话摘要**：
{turn_summary}

请输出更新后的草稿，严格 JSON：
{{"scratch": "...", "scratch_tokens": 450}}"""

# ------------------------------------------------------------------
# 辅助函数已移至模块顶部 (above class definitions)
# ------------------------------------------------------------------


class SimpleV3ContextHandler(BasePipelineContextHandler):
    """SimpleV3 上下文处理器——三区预算 + LLM 驱动压缩"""

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        super().__init__(session_id, workspace_path, config, session_task_handler)

        # self.config 已由基类绑定到 config["context"] 切片
        v3_cfg: dict[str, Any] = self.config.get("simple_v3", {})

        # 滑动窗口
        self.keep_groups: int = int(v3_cfg.get("keep_groups", 20))

        # 预算（M = token_budget）
        self.token_budget: int = int(v3_cfg.get("token_budget", 131_072))
        ratios: dict[str, float] = v3_cfg.get("budget_ratios", {})
        self.scratch_ratio: float = float(ratios.get("scratch", 0.10))
        self.summary_ratio: float = float(ratios.get("summary", 0.20))
        self.before_loop_ratio: float = float(ratios.get("before_loop", 0.75))
        self.warning_ratio: float = float(ratios.get("warning", 0.85))
        self.critical_ratio: float = float(ratios.get("critical", 0.95))

        # 压缩配置
        compress_cfg: dict[str, Any] = v3_cfg.get("compress", {})
        self.compress_llm_id: str = compress_cfg.get("llm_id", "gpt-4o-mini")
        self.compress_max_tokens: int = int(compress_cfg.get("max_tokens", 2000))
        self.compress_timeout_s: float = float(compress_cfg.get("timeout_s", 8.0))

        # Scratch 配置
        scratch_cfg: dict[str, Any] = v3_cfg.get("scratch", {})
        self.min_turn_tokens_for_update: int = int(
            scratch_cfg.get("min_turn_tokens_for_update", 200)
        )
        self.scratch_max_tokens: int = int(scratch_cfg.get("max_tokens", 6000))

        # 状态文件配置
        state_cfg: dict[str, Any] = v3_cfg.get("state", {})
        self._state_manager = SimpleV3StateManager(
            workspace_path=workspace_path,
            state_dirname=state_cfg.get("state_dirname", ".simple_v3"),
            state_filename=state_cfg.get("state_filename", "state.json"),
        )

        # Offload 存储
        offload_cfg: dict[str, Any] = v3_cfg.get("offload", {})
        self._offload_store = SimpleV3OffloadStore(
            workspace_path=workspace_path,
            subdir=offload_cfg.get("workspace_subdir", ".offload"),
        )
        # 工具结果 spill 最小 token 阈值（可配置，默认 5000）
        self.tool_result_spill_threshold: int = int(
            offload_cfg.get("tool_result_min_tokens", 5000)
        )

        # 本轮暂存
        self._pending_messages: list[dict[str, Any]] = []
        self._messages: list[UnifiedMessage] = []
        self._current_group_id: str | None = None
        # 本轮已 spill 的 tool_call_id 集合，防止同一工具结果循环 spill
        self._spilled_tool_ids: set[str] = set()

        # 最小保留 group 数（防线 2 的下限）
        self.min_keep_groups: int = 2

        # 上一轮的 scratch 更新 Future
        self._scratch_future: asyncio.Task | None = None

        _rlog.info(
            f"session_{session_id}",
            f"[SimpleV3ContextHandler] 初始化 "
            f"(keep_groups={self.keep_groups}, "
            f"budget={self.token_budget}, "
            f"ratios=scratch:{self.scratch_ratio}/summary:{self.summary_ratio}/"
            f"before:{self.before_loop_ratio}/warn:{self.warning_ratio}/"
            f"crit:{self.critical_ratio}, "
            f"compress_llm={self.compress_llm_id})",
        )

    # ==================================================================
    # BaseContextHandlerV3 生命周期
    # ==================================================================

    async def handle_before_loop(
        self, user_msg: dict[str, Any], max_rounds: int
    ) -> dict[str, Any]:
        """构建三区上下文：scratch + summaries + verbatim groups"""
        if not self._session_task_handler:
            raise ValueError("[SimpleV3ContextHandler] SessionTaskHandlerV2 未设置")

        # 1. 等待上一轮 scratch Future
        await self._await_scratch_future(timeout=5.0)

        # 清空本轮 spill 去重集合
        self._spilled_tool_ids.clear()

        # 2. 确保 state.json 最新
        last_group = self._state_manager.get_last_summarized_group()
        all_group_ids = await self._get_all_group_ids_desc()
        groups_since = (
            self._split_after(all_group_ids, last_group)
            if last_group
            else all_group_ids
        )
        self._state_manager.ensure_state(
            self.session_id, groups_since, summarize_fn=None
        )

        # 3. 生成当前 group_id
        if "group_id" not in user_msg:
            self._current_group_id = self._generate_group_id()
            user_msg["group_id"] = self._current_group_id
        else:
            self._current_group_id = user_msg["group_id"]

        self._pending_messages.append(dict(user_msg))

        # 4. 构建 system prompt（含基础模板 + scratch + summaries）
        system = await self.get_system_prompt()

        # 5. 计算三区预算
        system_tokens = _estimate_tokens(system or "")
        usable = max(1, self.token_budget - system_tokens)
        scratch_limit = int(usable * self.scratch_ratio)
        summary_limit = int(usable * self.summary_ratio)
        before_loop_limit = int(usable * self.before_loop_ratio)

        # 6. 构建 messages：贪婪加载 verbatim groups
        self._messages = await self._build_verbatim_messages(before_loop_limit)

        # 7. 追加当前 user 消息
        raw_parts = user_msg.get("content_parts")
        if raw_parts:
            content_parts = [
                BasePipelineContextHandler.deserialize_content_part(p)
                for p in raw_parts
            ]
            user_unified = UnifiedMessage(role="user", content_parts=content_parts)
        else:
            user_unified = UnifiedMessage(
                role="user", text=user_msg.get("content", "")
            )
        self._messages.append(user_unified)

        # 8. 加载工具列表
        tool_list = self._build_tool_list()

        await self._session_task_handler.log_info(
            f"[SimpleV3ContextHandler] 上下文就绪: "
            f"system_tokens={system_tokens}, "
            f"scratch_limit={scratch_limit}, summary_limit={summary_limit}, "
            f"before_loop_limit={before_loop_limit}, "
            f"messages={len(self._messages)}, tools={len(tool_list)}"
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
        """暂存 assistant 消息。"""
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
        """无记忆工具，全部透传。"""
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
        """暂存工具结果，大工具结果自动 spill 到 OffloadStore。"""
        if not tool_results:
            return self._messages

        processed: list[ToolCallResult] = []
        for tr in tool_results:
            content = tr.content or ""
            # 跳过已是占位符的内容（防止二次 spill）
            if content.startswith("[内容已卸载:"):
                processed.append(tr)
                continue
            # 跳过本轮已 spill 过的 tool_call_id（防止循环 spill）
            if tr.tool_call_id and tr.tool_call_id in self._spilled_tool_ids:
                processed.append(tr)
                continue
            if _estimate_tokens(content) > self.tool_result_spill_threshold:
                # 大工具结果自动 spill
                key = f"{self._current_group_id or 'unknown'}/tool_{tr.tool_call_id}"
                try:
                    head = content[:400].replace("\n", " ")
                    tail = content[-100:].replace("\n", " ") if len(content) > 500 else ""
                    tool_label = f"[{tr.tool_name}] " if tr.tool_name else ""
                    summary = f"{tool_label}{head}{'... ' + tail if tail else '...'}"
                    asyncio.create_task(
                        self._offload_store.store(
                            key=key,
                            content=content,
                            content_type="tool_result",
                            summary=summary,
                            session_id=self.session_id,
                            group_id=self._current_group_id or "",
                        )
                    )
                    placeholder = self._offload_store.render_placeholder(
                        key=key,
                        summary=summary,
                        content_type="tool_result",
                        original_tokens=_estimate_tokens(content),
                    )
                    if tr.tool_call_id:
                        self._spilled_tool_ids.add(tr.tool_call_id)
                    processed.append(ToolCallResult(
                        tool_call_id=tr.tool_call_id,
                        tool_name=tr.tool_name,
                        content=placeholder,
                        content_parts=None,
                    ))
                    continue
                except Exception:
                    pass
            processed.append(tr)

        user_msg: dict[str, Any] = {
            "role": "user",
            "tool_results": [
                {
                    "tool_call_id": tr.tool_call_id,
                    "tool_name": tr.tool_name,
                    "content": tr.content,
                }
                for tr in processed
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
            tool_results=processed,
            text=last_call_prompt or None,
        )
        self._messages.append(user_unified)
        return self._messages

    async def handle_before_llm_call(
        self,
        messages: list[UnifiedMessage],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> list[UnifiedMessage]:
        """两级压缩防线：llm_mini 智能压缩 → 强制丢弃"""
        if not messages:
            return messages

        total = _estimate_tokens_all(messages)
        warning_line = int(self.token_budget * self.warning_ratio)
        critical_line = int(self.token_budget * self.critical_ratio)

        if total < warning_line:
            return messages  # ← fast path

        # ── 防线 1: llm_mini 智能压缩 ──
        await loop_task_handler.log_info(
            f"[SimpleV3ContextHandler] 防线1: 触发 llm_mini 压缩 "
            f"(total={total}, warning={warning_line}, critical={critical_line})"
        )

        try:
            decision = await asyncio.wait_for(
                self._call_compress_llm(messages, loop_task_handler),
                timeout=self.compress_timeout_s,
            )
            messages = await self._apply_compression(messages, decision)
            total = _estimate_tokens_all(messages)
        except asyncio.TimeoutError:
            await loop_task_handler.log_warning(
                "[SimpleV3ContextHandler] 防线1超时，进入防线2"
            )
        except Exception as e:
            await loop_task_handler.log_error(
                f"[SimpleV3ContextHandler] 防线1异常: {e}"
            )

        if total < critical_line:
            await loop_task_handler.log_info(
                f"[SimpleV3ContextHandler] 防线1成功: total={total} < critical={critical_line}"
            )
            return messages

        # ── 防线 2: 强制丢弃最旧 groups ──
        await loop_task_handler.log_warning(
            f"[SimpleV3ContextHandler] 防线2: 强制丢弃 "
            f"(total={total}, critical={critical_line})"
        )

        group_ids = self._extract_group_ids_from_messages(messages)
        dropped = 0
        while (
            total >= critical_line
            and len(group_ids) > self.min_keep_groups
        ):
            oldest = group_ids.pop(0)
            messages = self._drop_group_messages(messages, oldest)
            total = _estimate_tokens_all(messages)
            dropped += 1

        await loop_task_handler.log_info(
            f"[SimpleV3ContextHandler] 防线2完成: "
            f"dropped={dropped} groups, total={total}"
        )

        return messages

    async def handle_after_loop(self, final_message: dict[str, Any]) -> None:
        """批量写盘 + fire-and-forget scratch 更新。"""
        # 附加 group_id
        if self._current_group_id and "group_id" not in final_message:
            final_message["group_id"] = self._current_group_id

        # 去重追加 final_message
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
            if self._session_task_handler:
                await self._session_task_handler.log_info(
                    f"[SimpleV3ContextHandler] 写盘 {len(self._pending_messages)} 条 (ok={ok})"
                )

        # 短回答跳过 scratch 更新
        assistant_tokens = _estimate_tokens(
            final_message.get("content", "")
        )
        if assistant_tokens < self.min_turn_tokens_for_update:
            self._pending_messages.clear()
            self._messages.clear()
            self._current_group_id = None
            return

        # fire-and-forget scratch 更新
        group_id = self._current_group_id
        pending_snapshot = list(self._pending_messages)

        self._scratch_future = asyncio.create_task(
            self._update_scratch_async(group_id, pending_snapshot)
        )

        # 清空内存状态
        self._pending_messages.clear()
        self._messages.clear()
        self._current_group_id = None

    # ==================================================================
    # 系统提示词
    # ==================================================================

    async def get_system_prompt(self, user_text: str = "") -> str | None:
        """构建系统提示词：基础模板 + scratch + summaries"""
        from ...system_prompts.pipeline_v1 import PIPELINE_V1_SYSTEM_PROMPT

        prompt = PIPELINE_V1_SYSTEM_PROMPT.format(
            workspace_root=self.workspace_path.absolute().as_posix(),
            project_root=self.widget_config.get("ps_metadata", {}).get(
                "project_root", "."
            ),
        )

        # 注入 scratch
        usable = max(1, self.token_budget - _estimate_tokens(prompt))
        scratch_limit = int(usable * self.scratch_ratio)
        scratch_text = self._state_manager.get_scratch()
        if scratch_text:
            if _estimate_tokens(scratch_text) > scratch_limit:
                scratch_text = self._truncate_text(scratch_text, scratch_limit)
            prompt += "\n\n## 工作草稿\n" + scratch_text

        # 注入 summaries
        summary_limit = int(usable * self.summary_ratio)
        summaries_text = self._state_manager.get_summaries_for_context(summary_limit)
        if summaries_text:
            prompt += "\n\n" + summaries_text

        # 注入技能列表
        skills_info = await self._get_skills_info()
        if skills_info:
            prompt += "\n\n---\n\n" + skills_info

        # 当前时间
        prompt += (
            f"\n\n# 当前时间\n"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        )

        return prompt

    # ==================================================================
    # BaseContextHandler 抽象方法
    # ==================================================================

    async def get_recent_messages(self, count: int) -> list[dict[str, Any]]:
        """从历史文件读取最近消息，按 group_id 分组保留最近 keep_groups 组。"""
        if count <= 0:
            return []
        msgs = self.history_manager.load_all()
        grouped, dirty = await self._group_messages_by_id(msgs)
        await self._writeback_if_dirty(msgs, dirty)
        recent = self._take_recent_groups(grouped, self.keep_groups)
        while recent and recent[0].get("role") != "user":
            recent.pop(0)
        return recent

    # ==================================================================
    # 内部实现
    # ==================================================================

    # ── Scratch Future ──

    async def _await_scratch_future(self, timeout: float = 5.0) -> None:
        """等待上一轮 scratch 更新完成。"""
        if self._scratch_future and not self._scratch_future.done():
            try:
                await asyncio.wait_for(self._scratch_future, timeout=timeout)
            except asyncio.TimeoutError:
                _rlog.warning(
                    f"session_{self.session_id}",
                    "[SimpleV3ContextHandler] scratch Future 超时，使用旧 scratch",
                )
            except Exception:
                pass
        self._scratch_future = None

    async def _update_scratch_async(
        self, group_id: str | None, pending: list[dict[str, Any]]
    ) -> None:
        """后台执行：llm_mini 生成本轮摘要 + 更新 scratch → 写 state.json"""
        try:
            turn_summary = self._extract_turn_summary(pending)
            if not turn_summary:
                return

            current_scratch = self._state_manager.get_scratch()
            new_scratch = await self._call_summarize_llm(
                turn_summary, current_scratch
            )

            self._state_manager.update_after_turn(
                group_id=group_id or "",
                scratch=new_scratch,
                summary=turn_summary,
            )

            _rlog.info(
                f"session_{self.session_id}",
                f"[SimpleV3ContextHandler] scratch 更新完成 "
                f"(group={group_id}, scratch_tokens={_estimate_tokens(new_scratch)})",
            )
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[SimpleV3ContextHandler] scratch 更新失败: {e}",
            )

    def _extract_turn_summary(
        self, pending: list[dict[str, Any]]
    ) -> str:
        """从 pending_messages 提取本轮对话摘要。"""
        parts: list[str] = []
        for m in pending:
            role = m.get("role", "user")
            content = m.get("content", "") or ""
            if isinstance(content, list):
                texts = []
                for p in content:
                    if isinstance(p, dict):
                        texts.append(str(p.get("text", "")))
                    else:
                        texts.append(str(p))
                content = "\n".join(t for t in texts if t)
            if content:
                parts.append(f"[{role}] {str(content)[:300]}")
            tool_calls = m.get("tool_calls") or []
            for tc in tool_calls:
                parts.append(
                    f"  →tool_call {tc.get('name', '')} "
                    f"args={str(tc.get('arguments', ''))[:100]}"
                )
            tool_results = m.get("tool_results") or []
            for tr in tool_results:
                cc = str(tr.get("content", ""))[:200]
                parts.append(f"  ←tool_result {tr.get('tool_name', '')} {cc}")
        return "\n".join(parts)

    # ── 三区上下文构建 ──

    async def _build_verbatim_messages(
        self, token_limit: int
    ) -> list[UnifiedMessage]:
        """贪婪加载 verbatim groups 直到 token 接近上限。"""
        raw = await self.get_recent_messages(self.keep_groups)
        if not raw:
            return []

        # 按 group_id 分组
        groups_dict: dict[str, list[dict[str, Any]]] = {}
        for m in raw:
            gid = m.get("group_id", "__nogroup__")
            groups_dict.setdefault(gid, []).append(m)

        # 按 group_id 排序（字符串排序约等于时间排序）
        sorted_groups = sorted(groups_dict.items())

        collected_flat: list[dict[str, Any]] = []
        total_tokens = 0

        for _gid, msgs in sorted_groups:
            msg_tokens = sum(
                _estimate_dict_message_tokens(m) for m in msgs
            )
            if total_tokens + msg_tokens > token_limit and collected_flat:
                break
            collected_flat.extend(msgs)
            total_tokens += msg_tokens

        return self.records_to_unified_messages(collected_flat)

    # ── 两级压缩防线 ──

    async def _call_compress_llm(
        self,
        messages: list[UnifiedMessage],
        loop_task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any]:
        """调用 llm_mini 做压缩决策，返回 {"offload": [...], "evict": [...]}"""
        table = self._build_metadata_table(messages)
        critical_line = int(self.token_budget * self.critical_ratio)

        user_prompt = (
            f"## 上下文消息\n"
            f"当前 {len([m for m in messages if _estimate_message_tokens(m) > 0])} 条消息，"
            f"预估 {_estimate_tokens_all(messages)} tokens，"
            f"目标 ≤ {critical_line} tokens\n\n"
            f"{table}"
        )

        result = await self._request_llm_mini(
            briefing_task_handler=loop_task_handler,
            system=_COMPRESS_SYSTEM_PROMPT,
            user_text=user_prompt,
        )

        if not result:
            return {"offload": [], "evict": []}

        return self._parse_compress_decision(result)

    def _build_metadata_table(
        self, messages: list[UnifiedMessage]
    ) -> str:
        """构建 Markdown 消息元数据表格。"""
        lines = [
            "| idx | role | tokens | tool_calls | tool_results | preview |",
            "|-----|------|--------|------------|-------------|---------|",
        ]
        for idx, msg in enumerate(messages):
            role = msg.role or "unknown"
            tokens = _estimate_message_tokens(msg)

            tool_calls_str = "-"
            if msg.tool_calls:
                tool_calls_str = ", ".join(
                    tc.name or "?" for tc in msg.tool_calls
                )

            tool_results_str = "-"
            if msg.tool_results:
                tool_results_str = ", ".join(
                    tr.tool_name or "?" for tr in msg.tool_results
                )

            preview = (msg.text or "")[:200]
            if len(msg.text or "") > 200:
                preview += f" [TRUNCATED, total {len(msg.text)} chars]"
            preview = preview.replace("\n", " ").replace("|", "\\|")

            lines.append(
                f"| {idx} | {role} | {tokens} | {tool_calls_str} | "
                f"{tool_results_str} | {preview} |"
            )

        return "\n".join(lines)

    @staticmethod
    def _parse_compress_decision(llm_output: str) -> dict[str, Any]:
        """解析压缩 LLM 的 JSON 输出。"""
        text = llm_output.strip()
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return {"offload": [], "evict": []}

        try:
            data = json.loads(m.group(0))
            return {
                "offload": data.get("offload") or [],
                "evict": data.get("evict") or [],
            }
        except json.JSONDecodeError:
            return {"offload": [], "evict": []}

    async def _apply_compression(
        self,
        messages: list[UnifiedMessage],
        decision: dict[str, Any],
    ) -> list[UnifiedMessage]:
        """按压缩决策修改 messages。"""
        # 收集要 evict 的索引
        evict_set: set = set(decision.get("evict", []) or [])

        # 应用 offload
        for item in decision.get("offload", []) or []:
            idx = item.get("msg_idx", -1)
            summary = item.get("summary", "")
            if 0 <= idx < len(messages) and idx not in evict_set:
                msg = messages[idx]
                content = msg.text or ""
                # 已是占位符，不可再次 offload
                if content.startswith("[内容已卸载:"):
                    continue
                if _estimate_tokens(content) > 200:
                    key = f"{self._current_group_id or 'unknown'}/{idx}"
                    try:
                        await self._offload_store.store(
                            key=key,
                            content=content,
                            content_type="text",
                            summary=summary,
                            session_id=self.session_id,
                            group_id=self._current_group_id or "",
                        )
                        placeholder = self._offload_store.render_placeholder(
                            key=key,
                            summary=summary,
                            content_type="text",
                            original_tokens=_estimate_tokens(content),
                        )
                        # 替换消息内容
                        new_msg = UnifiedMessage(
                            role=msg.role,
                            text=placeholder,
                            tool_calls=msg.tool_calls,
                            tool_results=msg.tool_results,
                            content_parts=msg.content_parts,
                        )
                        messages[idx] = new_msg
                    except Exception as e:
                        _rlog.error(
                            f"session_{self.session_id}",
                            f"[SimpleV3ContextHandler] offload 失败 idx={idx}: {e}",
                        )

        # 应用 evict（从后往前删以保持索引正确）
        for idx in sorted(evict_set, reverse=True):
            if 0 <= idx < len(messages):
                messages.pop(idx)

        return messages

    def _extract_group_ids_from_messages(
        self, messages: list[UnifiedMessage]
    ) -> list[str]:
        """从消息列表提取 group_id 列表（通过 pending_messages 关联）。"""
        # 简化实现：从 pending_messages 提取唯一 group_id
        seen: list[str] = []
        for pm in self._pending_messages:
            gid = pm.get("group_id", "")
            if gid and gid not in seen:
                seen.append(gid)
        return seen

    @staticmethod
    def _drop_group_messages(
        messages: list[UnifiedMessage], group_id: str
    ) -> list[UnifiedMessage]:
        """丢弃属于指定 group 的消息（简化实现：从头丢弃直到 removed 等于 group 条数）"""
        # 简化：丢弃前 N 条消息（最老的）
        # 完整实现需要按 group_id 定位精确丢弃
        if messages:
            messages.pop(0)
        return messages

    # ── Scratch 更新 (after_loop) ──

    async def _call_summarize_llm(
        self, turn_summary: str, current_scratch: str
    ) -> str:
        """调用 llm_mini 更新 scratch。"""
        max_chars = self.scratch_max_tokens * 2
        system = _SCRATCH_SYSTEM_PROMPT.format(
            max_tokens=self.scratch_max_tokens,
            max_chars=max_chars,
            current_scratch=current_scratch or "（无）",
            turn_summary=turn_summary,
        )

        summarize_task_handler = await self._session_task_handler.create_sibling_task(  # type: ignore[arg-type]
            task_type=TaskType.MEMORY_BRIEF,
            name="SimpleV3 Memory Briefing",
            metadata={},
        )

        # 使用简单的 user message 触发
        result = await self._request_llm_mini(
            briefing_task_handler=summarize_task_handler,  # type: ignore[arg-type]
            system=system,
            user_text="请根据上面的本轮对话摘要，更新草稿。",
        )

        if result:
            await summarize_task_handler.log_info(
                f"[SimpleV3ContextHandler] summarize llm 输出: {result[:50]}"
            )
            await summarize_task_handler.complete()
        else:
            await summarize_task_handler.fail()

        if not result:
            return current_scratch

        try:
            m = re.search(r"\{.*\}", result.strip(), flags=re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                return data.get("scratch", current_scratch)
        except json.JSONDecodeError:
            pass

        return current_scratch

    # ── LLM 调用 ──

    async def _request_llm_mini(
        self,
        briefing_task_handler: SessionTaskHandlerV2,
        system: str,
        user_text: str,
    ) -> str | None:
        """调用 llm_mini（单次，无工具），返回文本或 None。"""
        try:
            from ....llm import UnifiedMessage as UM
            from ....security_executor.handler import SecurityHandler

            security = SecurityHandler.get_instance()
            result = await security.request_llm_call_v3(
                session_task_handler=briefing_task_handler,
                llm_id=self.compress_llm_id,
                system=system,
                messages=[UM(role="user", text=user_text)],
                max_tokens=self.compress_max_tokens,
            )
            if result.get("success") and result.get("v2_response"):
                return result["v2_response"].text
            error = result.get("error", "unknown")
            await briefing_task_handler.log_error(
                f"[SimpleV3ContextHandler] llm_mini 调用失败: {error}"
            )
            return None
        except Exception as e:
            await briefing_task_handler.log_error(
                f"[SimpleV3ContextHandler] llm_mini 调用异常: {e}\n{traceback.format_exc()}",
            )
            return None

    # ── 辅助 ──

    def _generate_group_id(self) -> str:
        """生成唯一的对话组 ID。"""
        import uuid
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uuid_suffix = uuid.uuid4().hex[:6]
        return f"g_{timestamp}_{uuid_suffix}"

    async def _get_all_group_ids_desc(self) -> list[str]:
        """从 history 获取所有 group_id（降序）。"""
        msgs = self.history_manager.load_all()
        seen: list[str] = []
        for m in reversed(msgs):
            gid = m.get("group_id", "")
            if gid and gid not in seen:
                seen.append(gid)
        return seen

    @staticmethod
    def _split_after(items: list[str], pivot: str) -> list[str]:
        """返回 pivot 之后的项目列表。"""
        try:
            idx = items.index(pivot)
            return items[:idx]  # items 是降序，pivot 之后是更新的（更靠前）
        except ValueError:
            return items

    def _truncate_text(self, text: str, token_limit: int) -> str:
        """截断文本到指定 token 数以下。"""
        char_limit = token_limit * 2
        if len(text) <= char_limit:
            return text
        return text[:char_limit] + "\n... (内容过长已截断)"

    async def _get_skills_info(self) -> str:
        """获取可用技能列表。"""
        try:
            from ....skill import get_skill_manager

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
                f"[SimpleV3ContextHandler] 获取技能信息失败: {e}",
            )
            return ""

    def _build_tool_list(self) -> list[ToolDefinition]:
        """从全局 ToolManager 加载工具定义列表。"""
        from ....tool import get_tool_manager

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
                    f"[SimpleV3ContextHandler] 跳过工具 '{tool_name}': {exc}",
                )

        return tool_defs

    async def _group_messages_by_id(
        self, messages: list[dict[str, Any]]
    ) -> tuple:
        """按 group_id 分组消息（复用 PipelineWindow 逻辑）。"""
        import uuid
        from collections import OrderedDict

        groups: OrderedDict = OrderedDict()
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
                group_id = last_group_id
                msg["group_id"] = group_id
                dirty = True

            groups.setdefault(group_id, []).append(msg)

        return list(groups.values()), dirty

    async def _writeback_if_dirty(
        self, msgs: list[dict[str, Any]], dirty: bool
    ) -> None:
        """如有消息被补充 group_id，回写历史文件。"""
        if not dirty:
            return
        self.history_manager.save_all(msgs)
        if self._session_task_handler:
            await self._session_task_handler.log_info(
                "[SimpleV3ContextHandler] 已将推断的 group_id 回写历史文件"
            )

    @staticmethod
    def _take_recent_groups(
        groups: list[list[dict[str, Any]]], keep: int
    ) -> list[dict[str, Any]]:
        """保留最近 keep 组，展平。"""
        if not groups:
            return []
        recent = groups[-keep:] if len(groups) > keep else groups
        return [msg for group in recent for msg in group]
