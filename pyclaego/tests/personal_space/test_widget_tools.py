"""Tests for widget-aware tools (Phase 3.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaego.personal_space.datastores import JsonlStore
from pyclaego.personal_space.widget_tools import (
    WidgetDbQueryTool,
    WidgetDbWriteTool,
    WidgetEmitTool,
    build_widget_tools,
)
from pyclaego.tool.base_tool import ToolStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def jsonl_store(tmp_path: Path):
    store = JsonlStore(workspace_dir=tmp_path)
    await store.open()
    yield store
    await store.close()


# ---------------------------------------------------------------------------
# WidgetDbWriteTool / WidgetDbQueryTool
# ---------------------------------------------------------------------------


class TestDbTools:
    @pytest.mark.asyncio
    async def test_write_then_query(self, jsonl_store):
        write = WidgetDbWriteTool(
            {"tool_type": "widget_db_write", "tool_name": "widget_db_write"},
            store=jsonl_store,
            ps_id="alice",
            widget_id="w1",
        )
        query = WidgetDbQueryTool(
            {"tool_type": "widget_db_query", "tool_name": "widget_db_query"},
            store=jsonl_store,
            ps_id="alice",
            widget_id="w1",
        )

        wr = await write.execute(table="notes", rows=[{"x": 1}, {"x": 2}])
        assert wr.status is ToolStatus.SUCCESS
        assert wr.output["written"] == 2

        qr = await query.execute(table="notes", limit=10, descending=False)
        assert qr.status is ToolStatus.SUCCESS
        assert qr.output["count"] == 2
        assert [r["x"] for r in qr.output["rows"]] == [1, 2]

    @pytest.mark.asyncio
    async def test_write_validates_input(self, jsonl_store):
        write = WidgetDbWriteTool(
            {"tool_type": "widget_db_write", "tool_name": "widget_db_write"},
            store=jsonl_store,
            ps_id="alice",
            widget_id="w1",
        )
        r = await write.execute(table="notes", rows=[])
        assert r.status is ToolStatus.FAILED
        r = await write.execute(table="", rows=[{"x": 1}])
        assert r.status is ToolStatus.FAILED
        r = await write.execute(table="notes", rows=[{"x": 1}, "bad"])
        assert r.status is ToolStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_store_returns_failed(self):
        query = WidgetDbQueryTool(
            {"tool_type": "widget_db_query", "tool_name": "widget_db_query"},
            store=None,
            ps_id="alice",
            widget_id="w1",
        )
        r = await query.execute(table="notes")
        assert r.status is ToolStatus.FAILED
        assert "no store" in (r.error or "").lower()

    def test_get_description_hides_internal_fields(self, jsonl_store):
        for cls, name in [
            (WidgetDbQueryTool, "widget_db_query"),
            (WidgetDbWriteTool, "widget_db_write"),
        ]:
            tool = cls(
                {"tool_type": name, "tool_name": name},
                store=jsonl_store,
                ps_id="alice",
                widget_id="w1",
            )
            desc = tool.get_description()
            assert "name" in desc
            assert "parameters" in desc
            props = desc["parameters"]["properties"]
            for forbidden in ("widget_id", "ps_id", "store"):
                assert forbidden not in props


# ---------------------------------------------------------------------------
# WidgetEmitTool
# ---------------------------------------------------------------------------


class TestEmitTool:
    @pytest.mark.asyncio
    async def test_emit_persists_when_store_present(self, jsonl_store):
        tool = WidgetEmitTool(
            {"tool_type": "widget_emit", "tool_name": "widget_emit"},
            store=jsonl_store,
            ps_id="alice",
            widget_id="w1",
        )
        r = await tool.execute(channel="progress", payload={"step": 1})
        assert r.status is ToolStatus.SUCCESS
        assert r.output == {"delivered": False, "persisted": True}

        rows = await jsonl_store.query("_emits", descending=False)
        assert len(rows) == 1
        assert rows[0]["channel"] == "progress"
        assert rows[0]["payload"] == {"step": 1}
        assert rows[0]["ps_id"] == "alice"
        assert rows[0]["widget_id"] == "w1"

    @pytest.mark.asyncio
    async def test_emit_calls_emit_fn(self):
        captured = []

        async def emit_fn(event):
            captured.append(event)

        tool = WidgetEmitTool(
            {"tool_type": "widget_emit", "tool_name": "widget_emit"},
            store=None,
            ps_id="alice",
            widget_id="w1",
            emit_fn=emit_fn,
        )
        r = await tool.execute(channel="hi", payload={"a": 1})
        assert r.status is ToolStatus.SUCCESS
        assert r.output == {"delivered": True, "persisted": False}
        assert len(captured) == 1
        assert captured[0]["channel"] == "hi"

    @pytest.mark.asyncio
    async def test_emit_validates_input(self, jsonl_store):
        tool = WidgetEmitTool(
            {"tool_type": "widget_emit", "tool_name": "widget_emit"},
            store=jsonl_store,
            ps_id="alice",
            widget_id="w1",
        )
        r = await tool.execute(channel="", payload={})
        assert r.status is ToolStatus.FAILED
        r = await tool.execute(channel="x", payload="not-a-dict")
        assert r.status is ToolStatus.FAILED


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class TestBuilder:
    def test_no_store_only_emit_tool(self):
        tools = build_widget_tools(
            widget_config={}, store=None, ps_id="a", widget_id="w"
        )
        assert [t.tool_name for t in tools] == ["widget_emit"]

    @pytest.mark.asyncio
    async def test_full_tool_set_with_store(self, jsonl_store):
        tools = build_widget_tools(
            widget_config={}, store=jsonl_store, ps_id="a", widget_id="w"
        )
        names = [t.tool_name for t in tools]
        assert names == ["widget_db_query", "widget_db_write", "widget_emit"]

    @pytest.mark.asyncio
    async def test_disable_via_config(self, jsonl_store):
        tools = build_widget_tools(
            widget_config={"widget_tools": {"widget_db_write": {"enabled": False}}},
            store=jsonl_store,
            ps_id="a",
            widget_id="w",
        )
        names = [t.tool_name for t in tools]
        assert "widget_db_write" not in names
        assert "widget_db_query" in names
        assert "widget_emit" in names

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, jsonl_store):
        with pytest.raises(ValueError):
            build_widget_tools(
                widget_config={"widget_tools": {"web_fetch": {}}},
                store=jsonl_store,
                ps_id="a",
                widget_id="w",
            )
