"""Tests for Phase 8 (registry full + notes) and Phase 11 (WidgetHook reservation)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from pyclaego.personal_space import (
    Widget,
    WidgetClassRegistry,
    WidgetHook,
    WidgetManifest,
)
from pyclaego.personal_space.widget_classes.spec import WidgetClassSpec

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Phase 8.A — spec extensions
# ---------------------------------------------------------------------------


class TestSpecExtensions:
    async def test_resolves_store_schema_file_to_absolute(self, tmp_path: Path):
        cls_dir = tmp_path / "demo"
        cls_dir.mkdir()
        (cls_dir / "schema.sql").write_text("-- demo")
        (cls_dir / "widget_class.json").write_text(json.dumps({
            "class_id": "demo",
            "title": "Demo",
            "defaults": {
                "store": {"type": "sqlite", "schema_file": "schema.sql"}
            },
        }))

        reg = WidgetClassRegistry(builtin_root=tmp_path)
        spec = reg.get("demo")
        sf = spec.defaults["store"]["schema_file"]
        assert Path(sf).is_absolute()
        assert Path(sf).name == "schema.sql"
        assert Path(sf).exists()

    async def test_carries_default_viewers_and_cron_files(self, tmp_path: Path):
        cls_dir = tmp_path / "demo"
        cls_dir.mkdir()
        (cls_dir / "widget_class.json").write_text(json.dumps({
            "class_id": "demo",
            "title": "Demo",
            "default_viewers_file": "viewers.json",
            "default_cron_file": "cron.json",
        }))
        reg = WidgetClassRegistry(builtin_root=tmp_path)
        spec = reg.get("demo")
        assert spec.default_viewers_file == "viewers.json"
        assert spec.default_cron_file == "cron.json"

    async def test_resolve_asset(self, tmp_path: Path):
        cls_dir = tmp_path / "demo"
        cls_dir.mkdir()
        (cls_dir / "widget_class.json").write_text(json.dumps({"class_id": "demo"}))
        reg = WidgetClassRegistry(builtin_root=tmp_path)
        spec = reg.get("demo")
        p = spec.resolve_asset("prompts/main.system.md")
        assert p.is_absolute()
        assert p.parent == cls_dir / "prompts"


# ---------------------------------------------------------------------------
# Phase 8.B — notes builtin
# ---------------------------------------------------------------------------


class TestNotesWidgetClass:
    async def test_notes_class_loads_from_builtin(self):
        reg = WidgetClassRegistry()
        reg.load(force=True)
        assert "notes" in reg.list()
        spec = reg.get("notes")
        assert spec.class_id == "notes"
        assert spec.source == "builtin"
        # store 默认 sqlite + 已解析 schema_file 绝对路径
        store = spec.defaults.get("store")
        assert isinstance(store, dict)
        assert store["type"] == "sqlite"
        assert Path(store["schema_file"]).is_absolute()
        assert Path(store["schema_file"]).exists()
        # 工具默认开启
        tools = spec.defaults.get("tools") or {}
        assert tools.get("widget_db_query", {}).get("enabled") is True


# ---------------------------------------------------------------------------
# Phase 11 — WidgetHook reservation
# ---------------------------------------------------------------------------


_HOOK_PY = textwrap.dedent("""
    from pyclaego.personal_space.widget_classes import WidgetHook

    class WidgetHook(WidgetHook):
        async def on_create(self):
            self.widget._test_calls = ['create']

        async def on_chat(self, message, response=None):
            self.widget._test_calls.append('chat')

        async def on_destroy(self):
            self.widget._test_calls.append('destroy')

        def compute_highlight(self):
            return {'count': len(getattr(self.widget, '_test_calls', []))}
""")


class TestHookDiscovery:
    async def test_registry_picks_up_widget_class_py(self, tmp_path: Path):
        cls_dir = tmp_path / "withhook"
        cls_dir.mkdir()
        (cls_dir / "widget_class.json").write_text(json.dumps({
            "class_id": "withhook", "title": "WithHook"
        }))
        (cls_dir / "widget_class.py").write_text(_HOOK_PY)

        reg = WidgetClassRegistry(builtin_root=tmp_path)
        spec = reg.get("withhook")
        assert spec.hook_class is not None
        assert issubclass(spec.hook_class, WidgetHook)

    async def test_missing_widget_class_py_means_no_hook(self, tmp_path: Path):
        cls_dir = tmp_path / "nohook"
        cls_dir.mkdir()
        (cls_dir / "widget_class.json").write_text(json.dumps({
            "class_id": "nohook", "title": "NoHook"
        }))
        reg = WidgetClassRegistry(builtin_root=tmp_path)
        spec = reg.get("nohook")
        assert spec.hook_class is None


class _FakeAgent:
    async def process_v2(self, *, user_message, **_):
        return f"reply: {user_message['content']}"


class _FakeContext:
    def set_session_task_handler(self, h):
        pass

    def close(self):
        pass


class _FakeTaskHandler:
    def get_user_id(self):
        return "u"

    async def start(self):
        pass


def _spec_with_hook(tmp_path: Path) -> WidgetClassSpec:
    cls_dir = tmp_path / "withhook"
    cls_dir.mkdir()
    (cls_dir / "widget_class.json").write_text(json.dumps({
        "class_id": "withhook", "title": "WithHook"
    }))
    (cls_dir / "widget_class.py").write_text(_HOOK_PY)
    reg = WidgetClassRegistry(builtin_root=tmp_path)
    return reg.get("withhook")


class TestHookLifecycleInWidget:
    async def test_on_create_and_on_chat_and_on_destroy_called(self, tmp_path: Path):
        spec = _spec_with_hook(tmp_path)
        manifest = WidgetManifest(widget_id="w1", widget_class="withhook", title="W")
        widget = Widget(
            ps_id="alice",
            manifest=manifest,
            workspace_dir=tmp_path / "ws" / "w1",
            resolved_config={
                "agent": {"type": "fake"},
                "context": {"type": "fake"},
            },
            agent_factory=lambda *a, **k: _FakeAgent(),
            context_factory=lambda *a, **k: _FakeContext(),
            widget_class_spec=spec,
        )
        await widget.load()
        assert widget.hook is not None
        assert getattr(widget, "_test_calls") == ["create"]

        th = _FakeTaskHandler()
        await widget.process_message(
            {"content": "hi"}, source="chat", request_id="r", task_handler=th
        )
        assert widget._test_calls == ["create", "chat"]

        await widget.unload()
        assert widget._test_calls == ["create", "chat", "destroy"]

    async def test_widget_without_hook_works_normally(self, tmp_path: Path):
        manifest = WidgetManifest(widget_id="w1", widget_class="chat", title="W")
        widget = Widget(
            ps_id="alice",
            manifest=manifest,
            workspace_dir=tmp_path / "ws" / "w1",
            resolved_config={
                "agent": {"type": "fake"},
                "context": {"type": "fake"},
            },
            agent_factory=lambda *a, **k: _FakeAgent(),
            context_factory=lambda *a, **k: _FakeContext(),
        )
        await widget.load()
        assert widget.hook is None
        await widget.unload()
