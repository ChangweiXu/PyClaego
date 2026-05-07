"""SoulV6MemoryWriteReview — 写入前的冲突检查 & 审计

在 LLM 调用 memory_save_case / memory_save_experience 时，V6 在落盘之前先：
  1) 用 FTS 召回潜在重复 / 冲突的现有记忆（同 topic 或高相似 title/content）
  2) 计算冲突分（Jaccard + topic 命中）
  3) 三种动作：
       allow         —— 直接放行
       allow_with_link —— 放行但在新内容头注 `> 注意：与 [...] 相关`
       block_pending —— 返回需要用户/LLM 确认的提示，不落盘

设计目标：让模型自己在召回到冲突时主动改写或显式 override，而非系统自动覆盖。

返回的 `SoulV6WriteReviewResult` 由调用方（V6 handler）转成工具响应。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..logging import get_running_log
from .soulv6_memory_manager import SoulV6MemoryManager

if TYPE_CHECKING:
    from .soulv5_memory_manager import SearchResult

_rlog = get_running_log()


class SoulV6WriteAction(str, Enum):
    ALLOW = "allow"
    ALLOW_WITH_LINK = "allow_with_link"
    BLOCK_PENDING = "block_pending"


@dataclass
class SoulV6WriteReviewResult:
    action: SoulV6WriteAction
    conflicts: list[dict[str, Any]] = field(default_factory=list)  # [{md_path, title, score, reason}]
    annotated_content: str | None = None
    block_message: str | None = None

    def to_tool_response(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "conflicts": self.conflicts,
            "block_message": self.block_message,
        }


class SoulV6MemoryWriteReview:
    """memory_save_* 写前审查器"""

    def __init__(
        self,
        memory_manager: SoulV6MemoryManager,
        *,
        block_threshold: float = 0.85,
        link_threshold: float = 0.5,
        max_candidates: int = 8,
        require_explicit_override: bool = True,
    ) -> None:
        self._mm = memory_manager
        self.block_threshold = block_threshold
        self.link_threshold = link_threshold
        self.max_candidates = max_candidates
        self.require_explicit_override = require_explicit_override

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def review_save(
        self,
        *,
        title: str,
        content: str,
        topic: str,
        tags: list[str] | None = None,
        doc_type_hint: str = "case",
        override_conflict: bool = False,
    ) -> SoulV6WriteReviewResult:
        title = (title or "").strip()
        content = (content or "").strip()

        # 显式 override 跳过审查
        if override_conflict and not self.require_explicit_override:
            return SoulV6WriteReviewResult(action=SoulV6WriteAction.ALLOW)

        try:
            candidates = await self._find_conflict_candidates(
                title, content, topic, doc_type_hint
            )
        except Exception as e:
            _rlog.error(
                "soulv6_write_review",
                f"[SoulV6MemoryWriteReview] 召回候选失败: {e}",
            )
            return SoulV6WriteReviewResult(action=SoulV6WriteAction.ALLOW)

        if not candidates:
            return SoulV6WriteReviewResult(action=SoulV6WriteAction.ALLOW)

        # 评分
        scored: list[dict[str, Any]] = []
        for c in candidates:
            score = self._similarity(title, content, topic, c)
            scored.append({
                "md_path": c.md_path,
                "title": c.title,
                "doc_type": c.doc_type,
                "score": round(score, 3),
                "reason": self._reason_for(score, title, c),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[: self.max_candidates]
        top_score = top[0]["score"]

        if override_conflict:
            return SoulV6WriteReviewResult(
                action=SoulV6WriteAction.ALLOW,
                conflicts=top,
            )

        if top_score >= self.block_threshold:
            msg = self._build_block_message(top)
            return SoulV6WriteReviewResult(
                action=SoulV6WriteAction.BLOCK_PENDING,
                conflicts=top,
                block_message=msg,
            )

        if top_score >= self.link_threshold:
            annotated = self._annotate_with_links(content, top)
            return SoulV6WriteReviewResult(
                action=SoulV6WriteAction.ALLOW_WITH_LINK,
                conflicts=top,
                annotated_content=annotated,
            )

        return SoulV6WriteReviewResult(
            action=SoulV6WriteAction.ALLOW,
            conflicts=top,
        )

    # ------------------------------------------------------------------
    # 召回 + 评分
    # ------------------------------------------------------------------

    async def _find_conflict_candidates(
        self, title: str, content: str, topic: str, doc_type_hint: str
    ) -> list[SearchResult]:
        # 用 title + topic 作为查询
        query = f"{title} {topic}".strip()
        if not query:
            return []
        try:
            rows = await self._mm.query(
                query=query, doc_type=doc_type_hint, top_k=self.max_candidates
            )
        except Exception:
            rows = []
        # 也试试 experience 和 topic 类型作为补充
        if doc_type_hint != "experience":
            try:
                rows_exp = await self._mm.query(
                    query=query, doc_type="experience", top_k=3
                )
                rows = rows + list(rows_exp)
            except Exception:
                pass
        # 去重
        seen = set()
        out: list[SearchResult] = []
        for r in rows:
            if r.md_path in seen:
                continue
            seen.add(r.md_path)
            out.append(r)
        return out

    def _similarity(
        self, title: str, content: str, topic: str, cand: SearchResult
    ) -> float:
        a = self._token_set((title + " " + content).lower())
        b = self._token_set((cand.title + " " + (cand.snippet or "")).lower())
        if not a or not b:
            j = 0.0
        else:
            j = len(a & b) / len(a | b)
        title_match = 0.0
        if title and cand.title and title.lower() == cand.title.lower():
            title_match = 0.5
        elif title and cand.title and (
            title.lower() in cand.title.lower()
            or cand.title.lower() in title.lower()
        ):
            title_match = 0.25
        topic_match = 0.0
        if topic and any(t.lower() == topic.lower() for t in (cand.tags or [])):
            topic_match = 0.15
        return min(1.0, j + title_match + topic_match)

    @staticmethod
    def _token_set(text: str) -> set:
        return set(re.findall(r"[\w\u4e00-\u9fff]{2,}", text or ""))

    @staticmethod
    def _reason_for(score: float, title: str, cand: SearchResult) -> str:
        if cand.title and title and cand.title.lower() == title.lower():
            return "标题完全相同"
        if cand.title and title and (
            title.lower() in cand.title.lower() or cand.title.lower() in title.lower()
        ):
            return "标题包含/被包含"
        if score >= 0.7:
            return "正文高度重叠"
        if score >= 0.5:
            return "正文部分重叠"
        return "弱相关"

    # ------------------------------------------------------------------
    # 渲染：阻塞消息 / 链接注释
    # ------------------------------------------------------------------

    def _build_block_message(self, top: list[dict[str, Any]]) -> str:
        lines = [
            "⚠️ 该写入与已有记忆高度重复/冲突，已暂缓落盘。",
            "请选择动作：",
            "  1) 修改 title/content 后重新调用同一工具；",
            "  2) 在调用参数中加入 override_conflict=true 以强制写入；",
            "  3) 改为更新现有记忆（memory_update / memory_deprecate）。",
            "",
            "潜在冲突项：",
        ]
        for c in top:
            lines.append(
                f"  - [{c['doc_type']}] {c['title']}  (score={c['score']}, {c['reason']})"
            )
            lines.append(f"    路径: {c['md_path']}")
        return "\n".join(lines)

    def _annotate_with_links(
        self, content: str, top: list[dict[str, Any]]
    ) -> str:
        if not top:
            return content
        link_lines = ["> 相关已有记忆（自动关联）："]
        for c in top[:3]:
            link_lines.append(f"> - [{c['title']}]({c['md_path']})")
        return "\n".join(link_lines) + "\n\n" + content
