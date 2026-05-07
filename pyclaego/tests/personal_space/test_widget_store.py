"""Tests for WidgetStore implementations (Phase 3.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaego.personal_space.datastores import (
    JsonlStore,
    SqliteStore,
    create_widget_store,
)

# ---------------------------------------------------------------------------
# JsonlStore
# ---------------------------------------------------------------------------


class TestJsonlStore:
    @pytest.mark.asyncio
    async def test_write_and_query_basic(self, tmp_path: Path):
        store = JsonlStore(workspace_dir=tmp_path)
        await store.open()
        try:
            n = await store.write("events", [{"kind": "a"}, {"kind": "b"}])
            assert n == 2
            rows = await store.query("events", limit=10, descending=False)
            assert len(rows) == 2
            assert [r["kind"] for r in rows] == ["a", "b"]
            # 自动注入 _id / _ts
            assert all("_id" in r and "_ts" in r for r in rows)
            assert rows[0]["_id"] < rows[1]["_id"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_where_filter(self, tmp_path: Path):
        store = JsonlStore(workspace_dir=tmp_path)
        await store.open()
        try:
            await store.write(
                "items",
                [
                    {"name": "a", "tag": "x"},
                    {"name": "b", "tag": "y"},
                    {"name": "c", "tag": "x"},
                ],
            )
            rows = await store.query("items", where={"tag": "x"}, descending=False)
            assert [r["name"] for r in rows] == ["a", "c"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_query_missing_table_returns_empty(self, tmp_path: Path):
        store = JsonlStore(workspace_dir=tmp_path)
        await store.open()
        try:
            rows = await store.query("does_not_exist")
            assert rows == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_schema_lists_tables(self, tmp_path: Path):
        store = JsonlStore(workspace_dir=tmp_path)
        await store.open()
        try:
            await store.write("a", [{"x": 1}])
            await store.write("b", [{"y": 1}, {"y": 2}])
            schema = await store.schema()
            assert schema["type"] == "jsonl"
            tables = {t["table"]: t["rows"] for t in schema["tables"]}
            assert tables == {"a": 1, "b": 2}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_write_before_open_raises(self, tmp_path: Path):
        store = JsonlStore(workspace_dir=tmp_path)
        with pytest.raises(RuntimeError):
            await store.write("t", [{"x": 1}])


# ---------------------------------------------------------------------------
# SqliteStore
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT
);
"""


class TestSqliteStore:
    @pytest.mark.asyncio
    async def test_open_creates_db_file(self, tmp_path: Path):
        store = SqliteStore(workspace_dir=tmp_path, options={"schema_sql": _SCHEMA})
        await store.open()
        try:
            assert (tmp_path / "data" / "widget.db").exists()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_write_and_query(self, tmp_path: Path):
        store = SqliteStore(workspace_dir=tmp_path, options={"schema_sql": _SCHEMA})
        await store.open()
        try:
            n = await store.write(
                "notes",
                [{"title": "a", "body": "hello"}, {"title": "b", "body": "world"}],
            )
            assert n == 2
            rows = await store.query("notes", order_by="id", descending=False)
            assert len(rows) == 2
            assert rows[0]["title"] == "a"
            assert rows[1]["title"] == "b"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_where_clause(self, tmp_path: Path):
        store = SqliteStore(workspace_dir=tmp_path, options={"schema_sql": _SCHEMA})
        await store.open()
        try:
            await store.write(
                "notes",
                [{"title": "a"}, {"title": "b"}, {"title": "a"}],
            )
            rows = await store.query("notes", where={"title": "a"})
            assert len(rows) == 2
            assert all(r["title"] == "a" for r in rows)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_schema_lists_tables_and_columns(self, tmp_path: Path):
        store = SqliteStore(workspace_dir=tmp_path, options={"schema_sql": _SCHEMA})
        await store.open()
        try:
            schema = await store.schema()
            assert schema["type"] == "sqlite"
            names = [t["table"] for t in schema["tables"]]
            assert "notes" in names
            cols = {c["name"]: c for c in next(t for t in schema["tables"] if t["table"] == "notes")["columns"]}
            assert {"id", "title", "body"} <= set(cols.keys())
            assert cols["id"]["pk"] is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_schema_file_option(self, tmp_path: Path):
        schema_path = tmp_path / "init.sql"
        schema_path.write_text(_SCHEMA, encoding="utf-8")
        store = SqliteStore(
            workspace_dir=tmp_path, options={"schema_file": "init.sql"}
        )
        await store.open()
        try:
            await store.write("notes", [{"title": "x"}])
            rows = await store.query("notes")
            assert len(rows) == 1
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_returns_none_when_no_store_config(self, tmp_path: Path):
        assert create_widget_store({}, tmp_path) is None
        assert create_widget_store({"store": {"type": "none"}}, tmp_path) is None

    def test_creates_jsonl(self, tmp_path: Path):
        store = create_widget_store({"store": {"type": "jsonl"}}, tmp_path)
        assert isinstance(store, JsonlStore)

    def test_creates_sqlite(self, tmp_path: Path):
        store = create_widget_store(
            {"store": {"type": "sqlite", "schema_sql": _SCHEMA}}, tmp_path
        )
        assert isinstance(store, SqliteStore)

    def test_unknown_type_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            create_widget_store({"store": {"type": "redis"}}, tmp_path)
