"""SoulV6StaleEvictor — 过时工具结果的驱逐管线

场景：
    主对话在一轮内可能多次调用工具，老轮次的工具输出会在后续轮次失去价值。
    V5 做法是等到 compact_session 整轮压缩；V6 采用更细粒度的"逐消息"策略：
    每次 LLM 调用前，扫描历史消息里的 tool_result，按启发式打分决定：

    KEEP      保持原样（近期 / 小 / 引用过）
    SUMMARIZE 用 head 片段替换 content（中等年龄 / 中等大小）
    DROP      只保留"占位符"，完整内容已在 ToolResultStore 可供按需读取

    本模块只返回 decision 列表，真正修改消息由 handler 完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from ..logging import get_running_log
from .soulv6_tool_result_store import SoulV6ToolResultStore, SoulV6StoredArtifact

_rlog = get_running_log()


class SoulV6EvictAction(str, Enum):
    KEEP = "keep"
    SUMMARIZE = "summarize"
    DROP = "drop"


@dataclass
class SoulV6EvictDecision:
    """单个 tool_result 的处置决策"""
    tool_call_id: str
    tool_name: str
    group_id: Optional[str]
    message_index: int              # 在 messages 列表中的位置
    current_tokens: int             # 现有 content 的 token 估算
    action: SoulV6EvictAction
    replacement_text: Optional[str] = None  # action != KEEP 时的新文本
    reason: str = ""                         # 人类可读的判定理由


class SoulV6StaleEvictor:
    """过时工具结果驱逐器

    决策输入（按优先级从高到低）：
    1. "固定" 的工具结果（用户 /pin 过）- 永远 KEEP
    2. 模型在近期文本中引用过的 tool_call_id - KEEP
    3. 工具结果所在 group 是当前 group - KEEP
    4. age（距今轮数）+ tokens 的联合启发式
    """

    def __init__(
        self,
        store: Optional[SoulV6ToolResultStore] = None,
        keep_recent_turns: int = 2,
        summarize_tokens_threshold: int = 1_000,
        drop_tokens_threshold: int = 4_000,
        head_chars_summary: int = 400,
    ) -> None:
        self._store = store or SoulV6ToolResultStore.get_instance()
        self.keep_recent_turns = keep_recent_turns
        self.summarize_tokens_threshold = summarize_tokens_threshold
        self.drop_tokens_threshold = drop_tokens_threshold
        self.head_chars_summary = head_chars_summary

    # ------------------------------------------------------------------
    # 核心决策
    # ------------------------------------------------------------------

    def decide(
        self,
        messages: List[Any],
        current_group_id: Optional[str],
        pinned_tool_call_ids: Optional[Set[str]] = None,
        referenced_tool_call_ids: Optional[Set[str]] = None,
    ) -> List[SoulV6EvictDecision]:
        """扫描消息列表，返回每个 tool_result 的处置决策

        Args:
            messages: UnifiedMessage 列表（按时间顺序）
            current_group_id: 当前对话轮的 group_id（用于"同组保留"规则）
            pinned_tool_call_ids: 用户 /pin 过的 tool_call_id 集合
            referenced_tool_call_ids: 近期文本中被引用的 tool_call_id 集合

        Returns:
            每个 tool_result（含 artifact 可查）的决策列表
        """
        pinned = pinned_tool_call_ids or set()
        referenced = referenced_tool_call_ids or set()

        # 定位最近 N 轮的 message index 起点
        assistant_turn_indices: List[int] = []
        for i, m in enumerate(messages):
            if getattr(m, "role", None) == "assistant":
                assistant_turn_indices.append(i)
        recent_cutoff_index = (
            assistant_turn_indices[-self.keep_recent_turns]
            if len(assistant_turn_indices) >= self.keep_recent_turns
            else 0
        )

        decisions: List[SoulV6EvictDecision] = []

        for idx, msg in enumerate(messages):
            tool_results = getattr(msg, "tool_results", None)
            if not tool_results:
                continue

            for tr in tool_results:
                tool_call_id = getattr(tr, "tool_call_id", None)
                if not tool_call_id:
                    continue

                tool_name = getattr(tr, "tool_name", "unknown")
                artifact: Optional[SoulV6StoredArtifact] = self._store.get_artifact(tool_call_id)
                group_id = artifact.group_id if artifact else None
                tokens = (
                    artifact.total_tokens
                    if artifact
                    else self._store.count_tokens(getattr(tr, "content", "") or "")
                )

                decisions.append(
                    self._decide_one(
                        idx=idx,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        group_id=group_id,
                        current_group_id=current_group_id,
                        tokens=tokens,
                        is_pinned=tool_call_id in pinned,
                        is_referenced=tool_call_id in referenced,
                        recent_cutoff_index=recent_cutoff_index,
                        artifact=artifact,
                    )
                )

        return decisions

    def _decide_one(
        self,
        *,
        idx: int,
        tool_call_id: str,
        tool_name: str,
        group_id: Optional[str],
        current_group_id: Optional[str],
        tokens: int,
        is_pinned: bool,
        is_referenced: bool,
        recent_cutoff_index: int,
        artifact: Optional[SoulV6StoredArtifact],
    ) -> SoulV6EvictDecision:
        # Rule 1: pinned → KEEP
        if is_pinned:
            return SoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                message_index=idx,
                current_tokens=tokens,
                action=SoulV6EvictAction.KEEP,
                reason="pinned by user",
            )

        # Rule 2: 模型近期引用过 → KEEP
        if is_referenced:
            return SoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                message_index=idx,
                current_tokens=tokens,
                action=SoulV6EvictAction.KEEP,
                reason="referenced by recent assistant message",
            )

        # Rule 3: 当前 group → KEEP
        if group_id and current_group_id and group_id == current_group_id:
            return SoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                message_index=idx,
                current_tokens=tokens,
                action=SoulV6EvictAction.KEEP,
                reason="current group",
            )

        # Rule 4: 在最近 N 轮内 → KEEP
        if idx >= recent_cutoff_index:
            return SoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                message_index=idx,
                current_tokens=tokens,
                action=SoulV6EvictAction.KEEP,
                reason=f"within last {self.keep_recent_turns} turns",
            )

        # Rule 5: 基于大小
        if tokens >= self.drop_tokens_threshold:
            return SoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                message_index=idx,
                current_tokens=tokens,
                action=SoulV6EvictAction.DROP,
                replacement_text=self._render_drop_placeholder(
                    tool_call_id, tool_name, tokens, artifact
                ),
                reason=f"stale & large ({tokens} tokens)",
            )

        if tokens >= self.summarize_tokens_threshold:
            return SoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                message_index=idx,
                current_tokens=tokens,
                action=SoulV6EvictAction.SUMMARIZE,
                replacement_text=self._render_summary(
                    tool_call_id, tool_name, tokens, artifact
                ),
                reason=f"stale & medium ({tokens} tokens)",
            )

        return SoulV6EvictDecision(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            group_id=group_id,
            message_index=idx,
            current_tokens=tokens,
            action=SoulV6EvictAction.KEEP,
            reason=f"small enough to keep ({tokens} tokens)",
        )

    # ------------------------------------------------------------------
    # 替代文本构造
    # ------------------------------------------------------------------

    def _render_drop_placeholder(
        self,
        tool_call_id: str,
        tool_name: str,
        tokens: int,
        artifact: Optional[SoulV6StoredArtifact],
    ) -> str:
        suffix = (
            f" Use `tool_result_read(tool_call_id=\"{tool_call_id}\", range=[s,e])` to read."
            if artifact
            else " (content no longer retrievable)"
        )
        return (
            f"[SoulV6 stale tool_result dropped — tool={tool_name}, "
            f"tool_call_id={tool_call_id}, original_tokens={tokens}]{suffix}"
        )

    def _render_summary(
        self,
        tool_call_id: str,
        tool_name: str,
        tokens: int,
        artifact: Optional[SoulV6StoredArtifact],
    ) -> str:
        if artifact:
            head = artifact.head_text[: self.head_chars_summary]
            return (
                f"[SoulV6 stale tool_result summarized — tool={tool_name}, "
                f"tool_call_id={tool_call_id}, original_tokens={tokens}]\n\n"
                f"---- HEAD (first {len(head)} chars) ----\n{head}\n\n"
                f"[Full content available via "
                f"tool_result_read(tool_call_id=\"{tool_call_id}\", range=[s,e])]"
            )
        return (
            f"[SoulV6 stale tool_result summarized — tool={tool_name}, "
            f"tool_call_id={tool_call_id}, original_tokens={tokens}] "
            f"(no artifact on disk)"
        )
