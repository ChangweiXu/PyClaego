"""SubAgentSoulV6StaleEvictor — 子 Agent 过时工具结果驱逐器

策略（按优先级）：
    1. 当前轮（current_round_idx）的 tool_result → KEEP
    2. 在最近 keep_recent_turns 轮内 → KEEP
    3. tokens >= drop_tokens_threshold → DROP（只保留占位符）
    4. tokens >= summarize_tokens_threshold → SUMMARIZE（保留 head 片段）
    5. 小结果 → KEEP

与 SoulV6StaleEvictor 的差异：
    - 无 "pin" 概念（子 Agent 没有 /pin 命令）
    - 无 "referenced" 概念（子 Agent 不追踪 assistant 文本中的 tool_call_id 引用）
    - 以 round_index（轮次编号）取代 group_id（会话分组）作为时序定位依据
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...logging import get_running_log
from .subagent_soulv6_artifact_store import (
    SubAgentSoulV6Artifact,
    SubAgentSoulV6ArtifactStore,
)

_rlog = get_running_log()


class SubAgentSoulV6EvictAction(str, Enum):
    KEEP = "keep"
    SUMMARIZE = "summarize"
    DROP = "drop"


@dataclass
class SubAgentSoulV6EvictDecision:
    """单个 tool_result 的处置决策"""
    tool_call_id: str
    tool_name: str
    message_index: int
    current_tokens: int
    action: SubAgentSoulV6EvictAction
    replacement_text: str | None = None
    reason: str = ""


class SubAgentSoulV6StaleEvictor:
    """子 Agent 过时工具结果驱逐器"""

    def __init__(
        self,
        store: SubAgentSoulV6ArtifactStore,
        keep_recent_turns: int = 2,
        summarize_tokens_threshold: int = 1_000,
        drop_tokens_threshold: int = 4_000,
        head_chars_summary: int = 400,
    ) -> None:
        self._store = store
        self.keep_recent_turns = keep_recent_turns
        self.summarize_tokens_threshold = summarize_tokens_threshold
        self.drop_tokens_threshold = drop_tokens_threshold
        self.head_chars_summary = head_chars_summary

    # ------------------------------------------------------------------
    # 核心决策
    # ------------------------------------------------------------------

    def decide(
        self,
        messages: list[Any],
        current_round_idx: int,
    ) -> list[SubAgentSoulV6EvictDecision]:
        """扫描消息列表，返回每个 tool_result 的处置决策

        Args:
            messages: UnifiedMessage 列表（按时间顺序）
            current_round_idx: 当前轮次编号（从 0 开始），用于界定"最近 N 轮"

        Returns:
            各 tool_result 的决策列表（仅包含 SUMMARIZE / DROP，KEEP 也会列入供日志）
        """
        # 定位最近 N 轮 assistant message 的起始 index
        assistant_indices: list[int] = [
            i for i, m in enumerate(messages)
            if getattr(m, "role", None) == "assistant"
        ]
        recent_cutoff_index = (
            assistant_indices[-self.keep_recent_turns]
            if len(assistant_indices) >= self.keep_recent_turns
            else 0
        )

        decisions: list[SubAgentSoulV6EvictDecision] = []

        for idx, msg in enumerate(messages):
            tool_results = getattr(msg, "tool_results", None)
            if not tool_results:
                continue

            for tr in tool_results:
                tool_call_id = getattr(tr, "tool_call_id", None)
                if not tool_call_id:
                    continue

                tool_name = getattr(tr, "tool_name", "unknown")
                artifact: SubAgentSoulV6Artifact | None = (
                    self._store.get_artifact(tool_call_id)
                )
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
                        tokens=tokens,
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
        tokens: int,
        recent_cutoff_index: int,
        artifact: SubAgentSoulV6Artifact | None,
    ) -> SubAgentSoulV6EvictDecision:
        # Rule 1+2: 在最近 N 轮内 → KEEP
        if idx >= recent_cutoff_index:
            return SubAgentSoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                message_index=idx,
                current_tokens=tokens,
                action=SubAgentSoulV6EvictAction.KEEP,
                reason=f"within last {self.keep_recent_turns} turns",
            )

        # Rule 3: 过大 → DROP
        if tokens >= self.drop_tokens_threshold:
            return SubAgentSoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                message_index=idx,
                current_tokens=tokens,
                action=SubAgentSoulV6EvictAction.DROP,
                replacement_text=self._render_drop_placeholder(
                    tool_call_id, tool_name, tokens, artifact
                ),
                reason=f"stale & large ({tokens} tokens)",
            )

        # Rule 4: 中等 → SUMMARIZE
        if tokens >= self.summarize_tokens_threshold:
            return SubAgentSoulV6EvictDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                message_index=idx,
                current_tokens=tokens,
                action=SubAgentSoulV6EvictAction.SUMMARIZE,
                replacement_text=self._render_summary(
                    tool_call_id, tool_name, tokens, artifact
                ),
                reason=f"stale & medium ({tokens} tokens)",
            )

        # Rule 5: 小结果 → KEEP
        return SubAgentSoulV6EvictDecision(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            message_index=idx,
            current_tokens=tokens,
            action=SubAgentSoulV6EvictAction.KEEP,
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
        artifact: SubAgentSoulV6Artifact | None,
    ) -> str:
        suffix = (
            f' Use `subagent_soulv6_tool_result_read(tool_call_id="{tool_call_id}")`'
            f" to read."
            if artifact
            else " (content no longer retrievable)"
        )
        return (
            f"[SubAgentSoulV6 stale tool_result dropped — tool={tool_name}, "
            f"tool_call_id={tool_call_id}, original_tokens={tokens}]{suffix}"
        )

    def _render_summary(
        self,
        tool_call_id: str,
        tool_name: str,
        tokens: int,
        artifact: SubAgentSoulV6Artifact | None,
    ) -> str:
        if artifact:
            head = artifact.head_text[: self.head_chars_summary]
            return (
                f"[SubAgentSoulV6 stale tool_result summarized — tool={tool_name}, "
                f"tool_call_id={tool_call_id}, original_tokens={tokens}]\n\n"
                f"---- HEAD (first {len(head)} chars) ----\n{head}\n\n"
                f'[Full content via `subagent_soulv6_tool_result_read('
                f'tool_call_id="{tool_call_id}")`]'
            )
        return (
            f"[SubAgentSoulV6 stale tool_result summarized — tool={tool_name}, "
            f"tool_call_id={tool_call_id}, original_tokens={tokens}] "
            f"(no artifact on disk)"
        )
