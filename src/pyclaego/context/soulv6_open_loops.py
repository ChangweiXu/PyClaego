"""SoulV6OpenLoopsStore — 未闭合问题/决策的持久化存储

一个 "open loop" 表示一轮对话中提出但尚未解决的事项，例如：
- 用户问了一个问题但 LLM 没有给出最终答案
- LLM 承诺"稍后再做 X"但还没做
- 某个 TODO / 未确认的假设

数据结构（每条 open loop）：
    {
        "id": "ol-20260423-a1b2",
        "session_id": "...",
        "created_at": "ISO",
        "updated_at": "ISO",
        "topic": "短描述",
        "description": "更长的描述",
        "source_group_id": "创建时所在 group",
        "status": "open" | "closed",
        "closed_at": "ISO" | None,
        "closed_reason": "..." | None,
    }

磁盘布局：
    .memory/soul_v6/open_loops/{session_id}.json
    → {"loops": [ ... ]}

所有写操作走 asyncio.Lock。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .soulv6_memory_manager import SoulV6MemoryManager
from ..logging import get_running_log

_rlog = get_running_log()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SoulV6OpenLoop:
    id: str
    session_id: str
    created_at: str
    updated_at: str
    topic: str
    description: str
    source_group_id: Optional[str] = None
    status: str = "open"
    closed_at: Optional[str] = None
    closed_reason: Optional[str] = None

    @classmethod
    def new(
        cls,
        session_id: str,
        topic: str,
        description: str,
        source_group_id: Optional[str] = None,
    ) -> "SoulV6OpenLoop":
        ts = _now()
        return cls(
            id=f"ol-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            session_id=session_id,
            created_at=ts,
            updated_at=ts,
            topic=topic,
            description=description,
            source_group_id=source_group_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SoulV6OpenLoopsStore:
    """Open loops 持久化存储（按 session 分文件）"""

    _instance: Optional["SoulV6OpenLoopsStore"] = None

    def __init__(self, memory_manager: Optional[SoulV6MemoryManager] = None) -> None:
        self._memory_manager = memory_manager or SoulV6MemoryManager.get_instance()
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # 进程内缓存：session_id → List[OpenLoop]
        self._cache: Dict[str, List[SoulV6OpenLoop]] = {}

    @classmethod
    def get_instance(cls) -> "SoulV6OpenLoopsStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def list_loops(
        self, session_id: str, status: Optional[str] = None
    ) -> List[SoulV6OpenLoop]:
        loops = await self._load(session_id)
        if status is None:
            return list(loops)
        return [l for l in loops if l.status == status]

    async def list_open(self, session_id: str) -> List[SoulV6OpenLoop]:
        return await self.list_loops(session_id, status="open")

    async def _load(self, session_id: str) -> List[SoulV6OpenLoop]:
        if session_id in self._cache:
            return self._cache[session_id]

        path = self._memory_manager.open_loops_path(session_id)
        if not path.exists():
            self._cache[session_id] = []
            return []
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            data = json.loads(raw)
            loops = [SoulV6OpenLoop(**item) for item in data.get("loops", [])]
        except Exception as e:
            _rlog.error(
                f"session_{session_id}",
                f"[SoulV6OpenLoopsStore] 加载失败 {path}: {e}",
            )
            loops = []
        self._cache[session_id] = loops
        return loops

    # ------------------------------------------------------------------
    # 写（加锁）
    # ------------------------------------------------------------------

    async def add(
        self,
        session_id: str,
        topic: str,
        description: str,
        source_group_id: Optional[str] = None,
    ) -> SoulV6OpenLoop:
        """添加一个 open loop。调用方应先做去重（按 topic 相似度）"""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            loops = await self._load(session_id)
            loop = SoulV6OpenLoop.new(
                session_id=session_id,
                topic=topic,
                description=description,
                source_group_id=source_group_id,
            )
            loops.append(loop)
            await self._persist(session_id, loops)
            return loop

    async def close(
        self,
        session_id: str,
        loop_id: str,
        reason: Optional[str] = None,
    ) -> bool:
        """关闭一个 open loop，返回是否成功"""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            loops = await self._load(session_id)
            for l in loops:
                if l.id == loop_id and l.status == "open":
                    l.status = "closed"
                    l.closed_at = _now()
                    l.closed_reason = reason
                    l.updated_at = l.closed_at
                    await self._persist(session_id, loops)
                    return True
            return False

    async def close_matching(
        self,
        session_id: str,
        topic_substr: str,
        reason: Optional[str] = None,
    ) -> int:
        """按 topic 子串匹配批量关闭，返回关闭数量"""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            loops = await self._load(session_id)
            closed_n = 0
            ts = _now()
            needle = topic_substr.lower()
            for l in loops:
                if l.status == "open" and needle in l.topic.lower():
                    l.status = "closed"
                    l.closed_at = ts
                    l.closed_reason = reason
                    l.updated_at = ts
                    closed_n += 1
            if closed_n:
                await self._persist(session_id, loops)
            return closed_n

    async def _persist(self, session_id: str, loops: List[SoulV6OpenLoop]) -> None:
        path = self._memory_manager.open_loops_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"loops": [l.to_dict() for l in loops]}
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(path.write_text, raw, "utf-8")
        self._cache[session_id] = loops

    # ------------------------------------------------------------------
    # 渲染：注入到 system prompt 的 open_loops tenant
    # ------------------------------------------------------------------

    async def render_for_context(
        self, session_id: str, token_budget: int
    ) -> Optional[str]:
        """返回一段供 system 中展示的 open loops 文本"""
        loops = await self.list_open(session_id)
        if not loops:
            return None
        lines = ["【尚未闭合的问题 / 承诺】"]
        for l in loops:
            lines.append(f"- [{l.id}] {l.topic}: {l.description}")
        text = "\n".join(lines)
        # 粗略 token 限制
        tc = self._memory_manager.token_counter
        if tc.count_tokens(text) > token_budget:
            # 按行截断
            kept: List[str] = [lines[0]]
            for line in lines[1:]:
                trial = "\n".join(kept + [line])
                if tc.count_tokens(trial) > token_budget:
                    break
                kept.append(line)
            text = "\n".join(kept + [f"... ({len(loops) - (len(kept) - 1)} more truncated)"])
        return text
