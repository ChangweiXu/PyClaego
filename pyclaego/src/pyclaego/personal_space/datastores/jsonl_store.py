"""JsonlStore — 追加写日志型 WidgetStore。

每个 ``table`` 对应一个 ``.jsonl`` 文件：``data/<table>.jsonl``。
每行一个 JSON 对象；写入时自动注入 ``_ts``（毫秒）与 ``_id``（自增计数）。

适用于：
- 事件流 / 操作日志 / 简单时间序列
- 不需要复杂查询的快速接入

不适用于：
- 大数据量、高并发查询（请用 :class:`SqliteStore`）
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .base import WidgetStore


def _safe_table_name(table: str) -> str:
    """把任意 ``table`` 映射成可安全用作文件名的字符串。"""
    cleaned = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in table)
    return cleaned or "default"


class JsonlStore(WidgetStore):
    """每个 table 一个 ``.jsonl`` 文件的追加写实现。"""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(workspace_dir=workspace_dir, options=options)
        self._data_dir: Path = self.workspace_dir / "data"
        self._lock: asyncio.Lock = asyncio.Lock()
        # 每个 table 的最近 _id（仅在内存里维持；启动时按文件行数初始化）
        self._next_id: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def open(self) -> None:
        if self._opened:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._opened = True

    async def close(self) -> None:
        self._opened = False
        # 没有持久句柄需要释放

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _path_for(self, table: str) -> Path:
        return self._data_dir / f"{_safe_table_name(table)}.jsonl"

    def _peek_next_id(self, table: str) -> int:
        """返回该 table 下一个 _id；按需从文件初始化计数。"""
        if table in self._next_id:
            return self._next_id[table]
        path = self._path_for(table)
        if not path.exists():
            self._next_id[table] = 1
            return 1
        # 数行数（粗略估计 _id 上限）
        try:
            with path.open("rb") as f:
                count = sum(1 for _ in f)
        except OSError:
            count = 0
        self._next_id[table] = count + 1
        return self._next_id[table]

    # ------------------------------------------------------------------
    # 数据操作
    # ------------------------------------------------------------------

    async def write(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not self._opened:
            raise RuntimeError("JsonlStore.write() called before open()")
        if not rows:
            return 0
        path = self._path_for(table)
        async with self._lock:
            now_ms = int(time.time() * 1000)
            base_id = self._peek_next_id(table)

            def _do_write() -> int:
                lines: list[str] = []
                for offset, row in enumerate(rows):
                    enriched = dict(row)
                    enriched.setdefault("_ts", now_ms)
                    enriched.setdefault("_id", base_id + offset)
                    lines.append(json.dumps(enriched, ensure_ascii=False))
                with path.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                return len(rows)

            written = await asyncio.to_thread(_do_write)
            self._next_id[table] = base_id + written
            return written

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        if not self._opened:
            raise RuntimeError("JsonlStore.query() called before open()")
        path = self._path_for(table)
        if not path.exists():
            return []

        def _do_query() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if where:
                        if not all(obj.get(k) == v for k, v in where.items()):
                            continue
                    results.append(obj)
            sort_key = order_by or "_ts"
            try:
                results.sort(key=lambda r: r.get(sort_key, 0), reverse=descending)
            except TypeError:
                # 字段类型不一致时退化为不排序
                pass
            if limit is not None and limit >= 0:
                results = results[:limit]
            return results

        return await asyncio.to_thread(_do_query)

    async def schema(self) -> dict[str, Any]:
        """列出当前 ``data/`` 下的所有 jsonl 表及其行数估计。"""
        tables: list[dict[str, Any]] = []
        if self._data_dir.exists():
            for p in sorted(self._data_dir.glob("*.jsonl")):
                try:
                    with p.open("rb") as f:
                        n = sum(1 for _ in f)
                except OSError:
                    n = 0
                tables.append({"table": p.stem, "rows": n})
        return {
            "type": "jsonl",
            "data_dir": str(self._data_dir),
            "tables": tables,
        }
