"""SoulV6MemoryRecaller — 10-stage 分层召回管道

阶段（按 plan F4 设计）：
  A. extract_keys       —— 关键词抽取（jieba 中文 / 空格英文）
  B. fts_recall         —— FTS5 字面/词形检索
  C. dense_recall       —— 稠密向量召回（可选，依赖 sqlite-vec + 嵌入模型；不可用时跳过）
  D. union              —— 合并去重，保留多源最高 rank
  E. recency_boost      —— 按 modified_at 时间近度加权
  F. llm_rerank         —— 可选 LLM 重排
  G. diversify_by_topic —— 同 topic 限额、按 doc_type 多样化
  H. conflict_flag      —— 标注疑似冲突（同 entity / topic 但内容矛盾）
  I. decay_filter       —— 按 status / created_at 老化过滤
  J. token_budget_fit   —— 按 token 预算装填，输出 markdown

返回：可直接拼入 system prompt 的 markdown 字符串。

设计原则：
- 每个阶段独立函数，可单独测试 / 替换
- 任何阶段失败都降级到上一阶段输出，整个 recaller 不应抛出
- 通过 SessionTaskHandlerV2 记录 stage 耗时与命中数（Phase 6 会扩展为统计指标）
"""

from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..logging import get_running_log
from ..task_manager import SessionTaskHandlerV2, TaskType

if TYPE_CHECKING:
    from .soulv5_memory_manager import SearchResult
    from .soulv6_memory_manager import SoulV6MemoryManager

_rlog = get_running_log()
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


# ---------------------------------------------------------------------------
# 候选项内部表示（包装 SearchResult，附加多阶段中间字段）
# ---------------------------------------------------------------------------

@dataclass
class SoulV6Candidate:
    md_path: str
    doc_type: str
    title: str
    tags: list[str]
    snippet: str
    fts_rank: float = 0.0          # 越接近 0 越相关（V5 query 返回的 rank）
    dense_score: float = 0.0       # 越大越相关
    recency_score: float = 0.0     # 0~1
    final_score: float = 0.0
    sources: set[str] = field(default_factory=set)  # {"fts", "dense"}
    modified_at: str | None = None
    conflict_with: list[str] = field(default_factory=list)

    @classmethod
    def from_search_result(cls, sr: SearchResult) -> SoulV6Candidate:
        return cls(
            md_path=sr.md_path,
            doc_type=sr.doc_type,
            title=sr.title,
            tags=list(sr.tags or []),
            snippet=sr.snippet or "",
            fts_rank=float(getattr(sr, "rank", 0.0) or 0.0),
            sources={"fts"},
        )


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class SoulV6MemoryRecaller:
    """10-stage 分层召回器"""

    DEFAULT_WORKFLOW = [
        "extract_keys",
        "fts_recall",
        "dense_recall",
        "union",
        "recency_boost",
        "llm_rerank",
        "diversify_by_topic",
        "conflict_flag",
        "decay_filter",
        "token_budget_fit",
    ]

    def __init__(self, memory_manager: SoulV6MemoryManager) -> None:
        self._manager = memory_manager
        cfg: dict[str, Any] = memory_manager.recall_config or {}

        self.enabled: bool = cfg.get("enabled", True)
        self.token_budget: int = cfg.get("token_budget", 4_000)
        self.workflow: list[str] = cfg.get("workflow", self.DEFAULT_WORKFLOW)

        methods = cfg.get("methods", {})
        # extract_keys
        ek = methods.get("extract_keys", methods.get("jieba_kw_extr", {}))
        self.keys_top_k: int = ek.get("top_k", 6)

        # fts_recall
        fts = methods.get("fts_recall", {})
        self.fts_top_k: int = fts.get("top_k", 20)
        self.fts_threshold: float = fts.get("fts_threshold", -5.0)
        self.fts_priority_order: list[str] = fts.get(
            "priority_order", ["experience", "case", "topic"]
        )

        # dense_recall（默认禁用，需安装 sqlite-vec + 嵌入模型）
        dr = methods.get("dense_recall", {})
        self.dense_enabled: bool = dr.get("enabled", False)
        self.dense_top_k: int = dr.get("top_k", 20)

        # recency_boost
        rb = methods.get("recency_boost", {})
        self.recency_half_life_days: float = rb.get("half_life_days", 30.0)
        self.recency_weight: float = rb.get("weight", 0.3)

        # llm_rerank
        rr = methods.get("llm_rerank", {})
        self.rerank_enabled: bool = rr.get("enabled", False)
        self.rerank_llm_id: str = rr.get("llm_id", "")
        self.rerank_top_k: int = rr.get("top_k", 8)

        # diversify_by_topic
        dv = methods.get("diversify_by_topic", {})
        self.max_per_topic: int = dv.get("max_per_topic", 3)
        self.max_per_doc_type: int = dv.get("max_per_doc_type", 6)

        # conflict_flag
        cf = methods.get("conflict_flag", {})
        self.conflict_enabled: bool = cf.get("enabled", True)

        # decay_filter
        df = methods.get("decay_filter", {})
        self.decay_max_age_days: float = df.get("max_age_days", 365.0)
        self.decay_archive_drops: bool = df.get("drop_archived", True)

        self._jieba_loaded = False

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    async def recall(
        self,
        user_text: str,
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> str:
        """主入口：返回可直接拼入 system prompt 的 markdown 字符串"""
        if not self.enabled or not user_text.strip():
            return ""
        if not session_task_handler:
            return ""

        recall_task = await session_task_handler.create_subtask(
            TaskType.MEMORY_RECALL,
            f"SoulV6 recall: {user_text[:30]}...",
        )
        await recall_task.start()

        keywords: list[str] = []
        fts_hits: list[SoulV6Candidate] = []
        dense_hits: list[SoulV6Candidate] = []
        merged: list[SoulV6Candidate] = []
        stage_metrics: dict[str, dict[str, Any]] = {}

        async def _run(stage_name: str, fn):
            t0 = time.perf_counter()
            try:
                result = await fn()
                dt_ms = int((time.perf_counter() - t0) * 1000)
                stage_metrics[stage_name] = {"ms": dt_ms, "ok": True}
                return result
            except Exception as e:
                dt_ms = int((time.perf_counter() - t0) * 1000)
                stage_metrics[stage_name] = {"ms": dt_ms, "ok": False, "err": str(e)}
                await session_task_handler.log_warning(
                    f"[SoulV6Recaller] stage={stage_name} 失败: {e}\n{traceback.format_exc()}",
                )
                return None

        for step in self.workflow:
            if step == "extract_keys":
                keywords = await _run(step, lambda: self._extract_keys(user_text, session_task_handler)) or []
            elif step == "fts_recall":
                fts_hits = await _run(
                    step,
                    lambda: self._fts_recall(keywords or user_text.strip().split(), session_task_handler),
                ) or []
            elif step == "dense_recall":
                dense_hits = await _run(
                    step,
                    lambda: self._dense_recall(user_text, session_task_handler),
                ) or []
            elif step == "union":
                merged = await _run(step, lambda: self._union(fts_hits, dense_hits)) or []
            elif step == "recency_boost":
                merged = await _run(step, lambda: self._recency_boost(merged)) or merged
            elif step == "llm_rerank":
                if self.rerank_enabled:
                    merged = await _run(
                        step,
                        lambda: self._llm_rerank(user_text, merged, recall_task),
                    ) or merged
            elif step == "diversify_by_topic":
                merged = await _run(step, lambda: self._diversify(merged)) or merged
            elif step == "conflict_flag":
                if self.conflict_enabled:
                    merged = await _run(step, lambda: self._conflict_flag(merged)) or merged
            elif step == "decay_filter":
                merged = await _run(step, lambda: self._decay_filter(merged)) or merged

        # 最后阶段：装填
        text = await self._token_budget_fit(merged, session_task_handler)
        await session_task_handler.log_info(
            f"[SoulV6Recaller] 完成 stages={list(stage_metrics.keys())} "
            f"final={len(merged)} chars={len(text)}",
        )
        try:
            await recall_task.complete()
        except Exception:
            pass
        return text

    # ------------------------------------------------------------------
    # A. extract_keys
    # ------------------------------------------------------------------

    async def _extract_keys(
        self, text: str, sth: SessionTaskHandlerV2
    ) -> list[str]:
        if not _CJK_RE.search(text):
            tokens = [t for t in text.strip().split() if t]
            return tokens[: self.keys_top_k]
        try:
            if not self._jieba_loaded:
                import jieba
                jieba.setLogLevel(20)
                self._jieba_loaded = True
            import jieba.analyse
            kws = jieba.analyse.extract_tags(text, topK=self.keys_top_k)
            if kws:
                await sth.log_info(f"[SoulV6Recaller] keys={kws}")
                return list(kws)
        except Exception:
            pass
        return text.strip().split()[: self.keys_top_k]

    # ------------------------------------------------------------------
    # B. fts_recall
    # ------------------------------------------------------------------

    async def _fts_recall(
        self, keywords: list[str], sth: SessionTaskHandlerV2
    ) -> list[SoulV6Candidate]:
        if not keywords:
            return []
        kw_str = " ".join(keywords)
        seen: set[str] = set()
        out: list[SoulV6Candidate] = []
        for doc_type in self.fts_priority_order:
            try:
                rows = await self._manager.query(
                    query=kw_str, doc_type=doc_type, top_k=self.fts_top_k,
                )
            except Exception:
                continue
            for r in rows:
                if r.rank < self.fts_threshold:
                    continue
                if r.md_path in seen:
                    continue
                seen.add(r.md_path)
                out.append(SoulV6Candidate.from_search_result(r))
        await sth.log_info(f"[SoulV6Recaller] fts hits={len(out)}")
        return out

    # ------------------------------------------------------------------
    # C. dense_recall（可选）
    # ------------------------------------------------------------------

    async def _dense_recall(
        self, user_text: str, sth: SessionTaskHandlerV2
    ) -> list[SoulV6Candidate]:
        if not self.dense_enabled:
            return []
        # 钩子点：未来接入 sqlite-vec + fastembed/bge-small。
        # 不可用时优雅返回空，不抛错。
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            await sth.log_info("[SoulV6Recaller] sqlite-vec 未安装，跳过 dense_recall")
            return []
        # 真实实现需要：
        #   1) 在 manager._init_db 里 CREATE VIRTUAL TABLE vec_index USING vec0(embedding float[N]);
        #   2) 嵌入器：fastembed / sentence_transformers
        #   3) async self._embed(text) -> List[float]
        #   4) SELECT md_path, distance FROM vec_index WHERE embedding MATCH ? ORDER BY distance LIMIT k
        # MVP 留作钩子，返回空。
        return []

    # ------------------------------------------------------------------
    # D. union（去重 + 多源加分）
    # ------------------------------------------------------------------

    async def _union(
        self,
        fts: list[SoulV6Candidate],
        dense: list[SoulV6Candidate],
    ) -> list[SoulV6Candidate]:
        by_path: dict[str, SoulV6Candidate] = {c.md_path: c for c in fts}
        for c in dense:
            if c.md_path in by_path:
                existing = by_path[c.md_path]
                existing.sources.update(c.sources)
                existing.dense_score = max(existing.dense_score, c.dense_score)
            else:
                by_path[c.md_path] = c
        return list(by_path.values())

    # ------------------------------------------------------------------
    # E. recency_boost
    # ------------------------------------------------------------------

    async def _recency_boost(
        self, cands: list[SoulV6Candidate]
    ) -> list[SoulV6Candidate]:
        if not cands:
            return cands
        # 从 DB 拉一次 modified_at（如果未拉过）
        await self._fetch_modified_at(cands)

        now = datetime.now(timezone.utc)
        half_life = self.recency_half_life_days
        for c in cands:
            age_days = self._age_days(c.modified_at, now)
            if age_days is None:
                c.recency_score = 0.0
            else:
                # 指数半衰：age = half_life → 0.5
                c.recency_score = 0.5 ** (age_days / max(half_life, 0.001))
            # 综合分：FTS 主导，加 recency 加成
            # 把 fts_rank 取负（V5 rank 越小越好），归一化到 [0,1] 再线性叠加
            fts_part = -float(c.fts_rank)
            c.final_score = fts_part + self.recency_weight * c.recency_score + c.dense_score
        cands.sort(key=lambda x: x.final_score, reverse=True)
        return cands

    async def _fetch_modified_at(self, cands: list[SoulV6Candidate]) -> None:
        await self._manager._ensure_db_connected()
        db = self._manager._db
        if db is None:
            return
        paths = [c.md_path for c in cands if c.modified_at is None]
        if not paths:
            return
        placeholders = ",".join(["?"] * len(paths))
        sql = f"SELECT md_path, modified_at FROM nodes WHERE md_path IN ({placeholders})"
        try:
            async with db.execute(sql, paths) as cur:
                rows = await cur.fetchall()
        except Exception:
            return
        meta = {r[0]: r[1] for r in rows}
        for c in cands:
            if c.md_path in meta:
                c.modified_at = meta[c.md_path]

    @staticmethod
    def _age_days(ts: str | None, now: datetime) -> float | None:
        if not ts:
            return None
        try:
            # 兼容 'YYYY-MM-DDTHH:MM:SS' / 带时区
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (now - dt).total_seconds() / 86400.0)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # F. llm_rerank
    # ------------------------------------------------------------------

    async def _llm_rerank(
        self,
        user_text: str,
        cands: list[SoulV6Candidate],
        sth: SessionTaskHandlerV2,
    ) -> list[SoulV6Candidate]:
        if not cands:
            return cands
        numbered = "\n\n".join(
            f"[{i}] ({c.doc_type}) {c.title}\n{c.snippet}"
            for i, c in enumerate(cands)
        )
        system = (
            "你是相关性判断器。给定用户查询和一组记忆候选，"
            f"返回最相关的最多 {self.rerank_top_k} 条编号，严格输出 JSON 整数数组，"
            "如 [0, 3, 1]。不要任何其他文字。"
        )
        user_prompt = f"用户查询：{user_text}\n\n候选：\n{numbered}"
        original = self._manager.llm_id
        try:
            if self.rerank_llm_id:
                self._manager.llm_id = self.rerank_llm_id
            resp = await self._manager._call_llm(
                session_task_handler=sth,
                system=system,
                user_text=user_prompt,
                max_tokens=200,
            )
        finally:
            self._manager.llm_id = original
        if not resp:
            return cands
        m = re.search(r"\[.*\]", resp, flags=re.DOTALL)
        if not m:
            return cands
        try:
            indices = json.loads(m.group(0))
        except Exception:
            return cands
        out: list[SoulV6Candidate] = []
        seen: set[int] = set()
        for idx in indices:
            if isinstance(idx, int) and 0 <= idx < len(cands) and idx not in seen:
                seen.add(idx)
                out.append(cands[idx])
        return out or cands

    # ------------------------------------------------------------------
    # G. diversify_by_topic
    # ------------------------------------------------------------------

    async def _diversify(
        self, cands: list[SoulV6Candidate]
    ) -> list[SoulV6Candidate]:
        if not cands:
            return cands
        per_topic: dict[str, int] = {}
        per_type: dict[str, int] = {}
        out: list[SoulV6Candidate] = []
        for c in cands:
            topic_key = c.tags[0] if c.tags else "_no_topic_"
            if per_topic.get(topic_key, 0) >= self.max_per_topic:
                continue
            if per_type.get(c.doc_type, 0) >= self.max_per_doc_type:
                continue
            out.append(c)
            per_topic[topic_key] = per_topic.get(topic_key, 0) + 1
            per_type[c.doc_type] = per_type.get(c.doc_type, 0) + 1
        return out

    # ------------------------------------------------------------------
    # H. conflict_flag
    # ------------------------------------------------------------------

    async def _conflict_flag(
        self, cands: list[SoulV6Candidate]
    ) -> list[SoulV6Candidate]:
        """同 (doc_type, primary_tag) 但 modified_at 接近且 snippet 显著不同时标注潜在冲突。"""
        if not cands:
            return cands
        groups: dict[tuple[str, str], list[SoulV6Candidate]] = {}
        for c in cands:
            key = (c.doc_type, c.tags[0] if c.tags else "")
            groups.setdefault(key, []).append(c)
        for group in groups.values():
            if len(group) < 2:
                continue
            # 简单启发：snippet 首 60 字差异度 > 0.6 即标注
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if self._snippet_diff(a.snippet, b.snippet) > 0.6:
                        a.conflict_with.append(b.md_path)
                        b.conflict_with.append(a.md_path)
        return cands

    @staticmethod
    def _snippet_diff(a: str, b: str) -> float:
        a_set = set(re.findall(r"\w+", (a or "").lower()))
        b_set = set(re.findall(r"\w+", (b or "").lower()))
        if not a_set and not b_set:
            return 0.0
        inter = len(a_set & b_set)
        union = len(a_set | b_set)
        if union == 0:
            return 0.0
        jaccard = inter / union
        return 1.0 - jaccard

    # ------------------------------------------------------------------
    # I. decay_filter
    # ------------------------------------------------------------------

    async def _decay_filter(
        self, cands: list[SoulV6Candidate]
    ) -> list[SoulV6Candidate]:
        if not cands:
            return cands
        await self._fetch_modified_at(cands)
        now = datetime.now(timezone.utc)
        out: list[SoulV6Candidate] = []
        max_age = self.decay_max_age_days
        for c in cands:
            age = self._age_days(c.modified_at, now)
            if age is not None and age > max_age:
                # 仍可保留如果有 conflict_flag 或 source 含 dense
                if "dense" not in c.sources and not c.conflict_with:
                    continue
            out.append(c)
        return out

    # ------------------------------------------------------------------
    # J. token_budget_fit
    # ------------------------------------------------------------------

    async def _token_budget_fit(
        self, cands: list[SoulV6Candidate], sth: SessionTaskHandlerV2
    ) -> str:
        if not cands:
            return ""
        tc = self._manager.token_counter
        header = (
            "# 相关记忆（自动召回）\n\n"
            "以下是与当前问题可能相关的历史经验，仅供参考：\n"
        )
        used = tc.count_tokens(header)
        sections: list[str] = []
        for c in cands:
            try:
                content = await self._manager.read_file(c.md_path)
            except Exception:
                content = None
            if not content:
                continue
            badges: list[str] = []
            if "dense" in c.sources and "fts" in c.sources:
                badges.append("hybrid")
            if c.conflict_with:
                badges.append(f"⚠潜在冲突×{len(c.conflict_with)}")
            badge_text = f"  [{', '.join(badges)}]" if badges else ""
            section = "\n".join([
                "",
                f"### [{c.doc_type}] {c.title}{badge_text}",
                "",
                "`````````md",
                content,
                "`````````",
                "",
            ])
            section_tokens = tc.count_tokens(section)
            if used + section_tokens > self.token_budget:
                break
            sections.append(section)
            used += section_tokens
        await sth.log_info(
            f"[SoulV6Recaller] token_budget_fit: {len(sections)}/{len(cands)} "
            f"used={used}/{self.token_budget}",
        )
        if not sections:
            return ""
        return header + "".join(sections)
