"""SqliteStore — 基于标准库 ``sqlite3`` 的 WidgetStore 实现。

设计要点：
- 单文件数据库：``data/widget.db``。
- 同步 API 用 ``asyncio.to_thread`` 包裹，避免阻塞事件循环。
- 建表语句来源（按优先级）：
  1. ``options["schema_sql"]``：直接的 SQL 字符串
  2. ``options["schema_file"]``：相对 ``workspace_dir`` 或绝对路径
  3. 缺省：什么都不建，调用方需自行 ``write`` 之前用 ``execute_script`` 建表
- ``write(table, rows)`` 自动按行的 keys 推导 ``INSERT`` 语句；
  调用方需保证表已存在且包含相应列。
- ``query(...)`` 支持等值 WHERE / LIMIT / ORDER BY；更复杂查询请调用
  :meth:`execute` 直接执行原始 SQL。

不依赖 aiosqlite，零新增依赖。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .base import WidgetStore


class SqliteStore(WidgetStore):
    """SQLite 后端的 WidgetStore。"""

    DEFAULT_DB_NAME = "widget.db"

    def __init__(
        self,
        *,
        workspace_dir: Path,
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(workspace_dir=workspace_dir, options=options)
        self._data_dir: Path = self.workspace_dir / "data"
        self._db_path: Path = self._data_dir / self.options.get(
            "db_name", self.DEFAULT_DB_NAME
        )
        self._conn: sqlite3.Connection | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def open(self) -> None:
        if self._opened:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)

        def _connect_and_init() -> sqlite3.Connection:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit 模式
            )
            conn.row_factory = sqlite3.Row
            # 务实默认值：开启外键 + WAL 提升并发读
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")

            schema_sql = self._resolve_schema_sql()
            if schema_sql:
                conn.executescript(schema_sql)
            return conn

        self._conn = await asyncio.to_thread(_connect_and_init)
        self._opened = True

    async def close(self) -> None:
        if not self._opened:
            return
        conn = self._conn
        self._conn = None
        self._opened = False
        if conn is not None:
            await asyncio.to_thread(conn.close)

    def _resolve_schema_sql(self) -> str | None:
        sql = self.options.get("schema_sql")
        if isinstance(sql, str) and sql.strip():
            return sql
        schema_file = self.options.get("schema_file")
        if not schema_file:
            return None
        path = Path(schema_file)
        if not path.is_absolute():
            path = self.workspace_dir / path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # 通用执行
    # ------------------------------------------------------------------

    async def execute(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """执行任意 SQL 并返回所有行（dict）。写语句返回空列表。"""
        if not self._opened or self._conn is None:
            raise RuntimeError("SqliteStore.execute() called before open()")
        conn = self._conn

        def _do() -> list[dict[str, Any]]:
            cur = conn.execute(sql, tuple(params or ()))
            try:
                rows = cur.fetchall()
            except sqlite3.ProgrammingError:
                rows = []
            return [dict(r) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def execute_script(self, sql_script: str) -> None:
        """执行多语句 SQL 脚本（建表/迁移用）。"""
        if not self._opened or self._conn is None:
            raise RuntimeError("SqliteStore.execute_script() called before open()")
        conn = self._conn
        async with self._lock:
            await asyncio.to_thread(conn.executescript, sql_script)

    # ------------------------------------------------------------------
    # 数据操作
    # ------------------------------------------------------------------

    async def write(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not self._opened or self._conn is None:
            raise RuntimeError("SqliteStore.write() called before open()")
        if not rows:
            return 0

        # 按第一行 keys 推导列；后续行必须有相同 keys（缺省取 None）
        cols = list(rows[0].keys())
        if not cols:
            return 0
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
        params_list = [tuple(r.get(c) for c in cols) for r in rows]

        conn = self._conn

        def _do() -> int:
            cur = conn.executemany(sql, params_list)
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        if not self._opened or self._conn is None:
            raise RuntimeError("SqliteStore.query() called before open()")

        clauses: list[str] = []
        params: list[Any] = []
        if where:
            for k, v in where.items():
                clauses.append(f'"{k}" = ?')
                params.append(v)
        sql = f'SELECT * FROM "{table}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            direction = "DESC" if descending else "ASC"
            sql += f' ORDER BY "{order_by}" {direction}'
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"

        return await self.execute(sql, params)

    async def schema(self) -> dict[str, Any]:
        """返回所有用户表 + 列定义。"""
        if not self._opened or self._conn is None:
            raise RuntimeError("SqliteStore.schema() called before open()")

        tables = await self.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        result_tables: list[dict[str, Any]] = []
        for t in tables:
            name = t["name"]
            cols = await self.execute(f'PRAGMA table_info("{name}")')
            result_tables.append(
                {
                    "table": name,
                    "columns": [
                        {
                            "name": c["name"],
                            "type": c["type"],
                            "not_null": bool(c["notnull"]),
                            "pk": bool(c["pk"]),
                        }
                        for c in cols
                    ],
                }
            )
        return {
            "type": "sqlite",
            "db_path": str(self._db_path),
            "tables": result_tables,
        }
