"""SQLite stats store: one row per call.

Single writer, async via aiosqlite. Schema is intentionally minimal —
aggregation queries can be built on top.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT    NOT NULL,
    protocol          TEXT    NOT NULL,
    alias             TEXT    NOT NULL,
    upstream_id       TEXT    NOT NULL,
    upstream_model    TEXT    NOT NULL,
    status            INTEGER,
    latency_ms        INTEGER,
    ttft_ms           INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    stream            INTEGER NOT NULL,
    error             TEXT,
    dump_path         TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_alias ON calls(alias);
CREATE INDEX IF NOT EXISTS idx_calls_ts    ON calls(ts);
"""


class StatsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def startup(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def shutdown(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                logger.exception("error closing stats db")
            self._db = None

    async def insert(
        self,
        *,
        protocol: str,
        alias: str,
        upstream_id: str,
        upstream_model: str,
        status: int | None,
        latency_ms: int | None,
        ttft_ms: int | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        stream: bool,
        error: str | None,
        dump_path: str | None,
    ) -> None:
        if self._db is None:
            return
        ts = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            try:
                await self._db.execute(
                    """
                    INSERT INTO calls (
                        ts, protocol, alias, upstream_id, upstream_model,
                        status, latency_ms, ttft_ms,
                        prompt_tokens, completion_tokens, total_tokens,
                        stream, error, dump_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts, protocol, alias, upstream_id, upstream_model,
                        status, latency_ms, ttft_ms,
                        prompt_tokens, completion_tokens, total_tokens,
                        1 if stream else 0, error, dump_path,
                    ),
                )
                await self._db.commit()
            except Exception:
                logger.exception("failed to insert call stats")
