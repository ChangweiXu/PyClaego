"""SoulV6EntityCardStore — 实体卡片存储

对每个被命名的实体（人、项目、专有名词、文件路径、URL 等）维护一份 JSON 卡片：
    .memory/soul_v6/entities/{slug}.json

卡片格式：
    {
        "slug": "soul_v6",
        "display_name": "SoulV6",
        "kind": "concept",   # person | project | concept | file | url | other
        "aliases": ["V6", "soulv6"],
        "facts": ["事实1", "事实2"],         # 简短陈述
        "first_seen_at": "ISO",
        "last_seen_at": "ISO",
        "mention_count": 12,
        "last_session_id": "...",
        "last_group_id": "...",
        "tags": ["..."],
        "links": {"docs": "..."}
    }

提供：
- `upsert(slug, ...)` —— 新建或合并字段（事实/别名去重）
- `get(slug)` —— 读卡
- `list_top_k(k, by="recent"|"mention")` —— 取热门卡
- `render_for_context(token_budget)` —— 拼成 markdown 注入 system

写入加 per-slug 锁。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .soulv6_memory_manager import SoulV6MemoryManager
from ..logging import get_running_log

_rlog = get_running_log()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SLUG_RE = re.compile(r"[^a-z0-9_\-\u4e00-\u9fff]+")


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = _SLUG_RE.sub("_", s)
    s = s.strip("_")
    return s or "_unnamed"


@dataclass
class SoulV6EntityCard:
    slug: str
    display_name: str
    kind: str = "concept"
    aliases: List[str] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    first_seen_at: str = ""
    last_seen_at: str = ""
    mention_count: int = 0
    last_session_id: Optional[str] = None
    last_group_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    links: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def render_compact(self) -> str:
        lines = [f"- **{self.display_name}** ({self.kind})"]
        if self.aliases:
            lines.append(f"  别名: {', '.join(self.aliases[:5])}")
        if self.facts:
            for f in self.facts[:5]:
                lines.append(f"  · {f}")
        if self.last_seen_at:
            lines.append(f"  最近提及: {self.last_seen_at[:10]} (共 {self.mention_count} 次)")
        return "\n".join(lines)


class SoulV6EntityCardStore:
    """实体卡片存储（按 slug 分文件）"""

    _instance: Optional["SoulV6EntityCardStore"] = None

    def __init__(self, memory_manager: Optional[SoulV6MemoryManager] = None) -> None:
        self._memory_manager = memory_manager or SoulV6MemoryManager.get_instance()
        self._slug_locks: Dict[str, asyncio.Lock] = {}
        self._cache: Dict[str, SoulV6EntityCard] = {}

    @classmethod
    def get_instance(cls) -> "SoulV6EntityCardStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def _path(self, slug: str) -> Path:
        return self._memory_manager.md_root / "entities" / f"{slug}.json"

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def get(self, slug: str) -> Optional[SoulV6EntityCard]:
        slug = slug.strip()
        if slug in self._cache:
            return self._cache[slug]
        path = self._path(slug)
        if not path.exists():
            return None
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            data = json.loads(raw)
            card = SoulV6EntityCard(**data)
            self._cache[slug] = card
            return card
        except Exception as e:
            _rlog.error(
                "soulv6_entities",
                f"[SoulV6EntityCardStore] 读取 {path} 失败: {e}",
            )
            return None

    async def list_all(self) -> List[SoulV6EntityCard]:
        d = self._memory_manager.md_root / "entities"
        if not d.exists():
            return []
        out: List[SoulV6EntityCard] = []
        for p in d.glob("*.json"):
            slug = p.stem
            c = await self.get(slug)
            if c is not None:
                out.append(c)
        return out

    async def list_top_k(
        self, k: int = 8, by: str = "recent"
    ) -> List[SoulV6EntityCard]:
        cards = await self.list_all()
        if by == "mention":
            cards.sort(key=lambda c: c.mention_count, reverse=True)
        else:  # recent
            cards.sort(key=lambda c: (c.last_seen_at or ""), reverse=True)
        return cards[:k]

    # ------------------------------------------------------------------
    # 写（加锁）
    # ------------------------------------------------------------------

    async def upsert(
        self,
        display_name: str,
        kind: str = "concept",
        new_facts: Optional[List[str]] = None,
        new_aliases: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        group_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        links: Optional[Dict[str, str]] = None,
    ) -> SoulV6EntityCard:
        slug = slugify(display_name)
        lock = self._slug_locks.setdefault(slug, asyncio.Lock())
        async with lock:
            card = await self.get(slug)
            now = _now()
            if card is None:
                card = SoulV6EntityCard(
                    slug=slug,
                    display_name=display_name,
                    kind=kind,
                    first_seen_at=now,
                )
            card.last_seen_at = now
            card.mention_count += 1
            if session_id:
                card.last_session_id = session_id
            if group_id:
                card.last_group_id = group_id
            if new_aliases:
                seen = set(a.lower() for a in card.aliases)
                for a in new_aliases:
                    if a and a.lower() not in seen and a.lower() != display_name.lower():
                        card.aliases.append(a)
                        seen.add(a.lower())
            if new_facts:
                seen = set(f.strip() for f in card.facts)
                for f in new_facts:
                    fs = f.strip()
                    if fs and fs not in seen:
                        card.facts.append(fs)
                        seen.add(fs)
            if tags:
                seen = set(card.tags)
                for t in tags:
                    if t and t not in seen:
                        card.tags.append(t)
                        seen.add(t)
            if links:
                card.links.update(links)
            await self._persist(card)
            return card

    async def _persist(self, card: SoulV6EntityCard) -> None:
        path = self._path(card.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(card.to_dict(), ensure_ascii=False, indent=2)
        await asyncio.to_thread(path.write_text, raw, "utf-8")
        self._cache[card.slug] = card

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    async def render_for_context(
        self, token_budget: int, top_k: int = 8
    ) -> Optional[str]:
        cards = await self.list_top_k(k=top_k, by="recent")
        if not cards:
            return None
        tc = self._memory_manager.token_counter
        header = "【实体卡片（按近度排序）】"
        kept: List[str] = [header]
        for c in cards:
            block = c.render_compact()
            trial = "\n".join(kept + [block])
            if tc.count_tokens(trial) > token_budget:
                break
            kept.append(block)
        if len(kept) <= 1:
            return None
        return "\n".join(kept)
