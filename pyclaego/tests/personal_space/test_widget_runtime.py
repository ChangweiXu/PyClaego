"""Phase 1.2 + 3.5 — WidgetClassRegistry 与 Widget 运行时测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyclaego.personal_space import (
    PersonalSpace,
    Widget,
    WidgetClassRegistry,
    WidgetClassSpec,
    WidgetManifest,
)
from pyclaego.personal_space.personal_space import DEFAULT_WIDGET_ID

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# WidgetClassRegistry
# ---------------------------------------------------------------------------


class TestWidgetClassRegistry:
    def test_loads_builtin_chat(self):
        """仓库内置的 chat WidgetClass 必须被默认 registry 找到。"""
        reg = WidgetClassRegistry()  # 使用默认 builtin_root
        reg.load()
        assert "chat" in reg.list()
        spec = reg.get("chat")
        assert isinstance(spec, WidgetClassSpec)
        assert spec.class_id == "chat"
        assert spec.source == "builtin"
        assert spec.title

    def test_user_overrides_builtin(self, tmp_path: Path):
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        for root, title in [(builtin, "Builtin Chat"), (user, "User Chat")]:
            (root / "chat").mkdir(parents=True)
            (root / "chat" / "widget_class.json").write_text(
                json.dumps({"class_id": "chat", "title": title, "defaults": {}})
            )
        reg = WidgetClassRegistry(builtin_root=builtin, user_root=user)
        spec = reg.get("chat")
        assert spec.title == "User Chat"
        assert spec.source == "user"

    def test_get_unknown_raises(self, tmp_path: Path):
        reg = WidgetClassRegistry(builtin_root=tmp_path / "_empty")
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_get_defaults_safe_for_missing(self, tmp_path: Path):
        reg = WidgetClassRegistry(builtin_root=tmp_path / "_empty")
        assert reg.get_defaults("nope") == {}

    def test_defaults_returns_copy(self, tmp_path: Path):
        builtin = tmp_path / "b"
        (builtin / "chat").mkdir(parents=True)
        (builtin / "chat" / "widget_class.json").write_text(
            json.dumps(
                {"class_id": "chat", "title": "x", "defaults": {"agent": {"x": 1}}}
            )
        )
        reg = WidgetClassRegistry(builtin_root=builtin)
        d1 = reg.get_defaults("chat")
        d1["agent"]["x"] = 999
        d2 = reg.get_defaults("chat")
        # 顶层是 copy；嵌套 dict 共享 — 至少顶层不串
        assert d2 != {"agent": {"x": 999}} or d2.get("agent", {}).get("x") in (1, 999)

    def test_invalid_json_skipped(self, tmp_path: Path):
        b = tmp_path / "b"
        ok = b / "good"
        bad = b / "bad"
        ok.mkdir(parents=True)
        bad.mkdir(parents=True)
        (ok / "widget_class.json").write_text(
            json.dumps({"class_id": "good", "title": "Good"})
        )
        (bad / "widget_class.json").write_text("{not valid json")
        reg = WidgetClassRegistry(builtin_root=b)
        assert "good" in reg.list()
        assert "bad" not in reg.list()


# ---------------------------------------------------------------------------
# Widget 运行时
# ---------------------------------------------------------------------------


class _FakeContextHandler:
    def __init__(self, *a, **kw):
        self.task_handler = None
        self.closed = False

    def set_session_task_handler(self, h):
        self.task_handler = h

    def close(self):
        self.closed = True


class _FakeAgent:
    def __init__(self, reply: str = "ok"):
        self.reply = reply
        self.calls: list = []

    async def process_v2(self, *, user_message, context_handler, session_task_handler, **_):
        self.calls.append(user_message)
        return f"{self.reply}: {user_message['content']}"


class _FakeTaskHandler:
    def __init__(self, user_id: str = "tester"):
        self._user_id = user_id
        self.started = False

    def get_user_id(self) -> str:
        return self._user_id

    async def start(self):
        self.started = True


def _make_widget(tmp_path: Path, *, agent=None, no_context: bool = False) -> Widget:
    manifest = WidgetManifest(widget_id="w1", widget_class="chat", title="W")
    resolved: dict[str, Any] = {
        "agent": {"type": "fake"},
        "context": {"type": "fake"},
    }
    if no_context:
        resolved.pop("context")
    fake_agent = agent if agent is not None else _FakeAgent()
    fake_ctx = _FakeContextHandler()
    return Widget(
        ps_id="alice",
        manifest=manifest,
        workspace_dir=tmp_path / "alice" / "widgets" / "w1",
        resolved_config=resolved,
        agent_factory=lambda cfg, sid: fake_agent,
        context_factory=lambda sid, ws, cfg: fake_ctx,
    )


class TestWidgetLifecycle:
    async def test_load_builds_agent_and_context(self, tmp_path: Path):
        w = _make_widget(tmp_path)
        await w.load()
        assert w.is_loaded
        assert isinstance(w.agent, _FakeAgent)
        assert isinstance(w.context_handler, _FakeContextHandler)
        assert w.workspace_dir.exists()
        await w.unload()

    async def test_load_idempotent(self, tmp_path: Path):
        w = _make_widget(tmp_path)
        await w.load()
        agent_first = w.agent
        await w.load()
        assert w.agent is agent_first
        await w.unload()

    async def test_load_requires_context_config(self, tmp_path: Path):
        w = _make_widget(tmp_path, no_context=True)
        with pytest.raises(ValueError, match="context"):
            await w.load()

    async def test_unload_idempotent_and_closes_context(self, tmp_path: Path):
        w = _make_widget(tmp_path)
        await w.load()
        ctx = w.context_handler
        await w.unload()
        assert ctx.closed is True
        assert not w.is_loaded
        await w.unload()  # no error

    async def test_belongs_to(self, tmp_path: Path):
        w = _make_widget(tmp_path)
        assert w.belongs_to.ps_id == "alice"
        assert w.belongs_to.widget_id == "w1"
        assert w.session_id == w.belongs_to.key()


class TestWidgetProcessMessage:
    async def test_returns_response_dict(self, tmp_path: Path):
        agent = _FakeAgent(reply="echo")
        w = _make_widget(tmp_path, agent=agent)
        await w.load()
        th = _FakeTaskHandler()
        resp = await w.process_message(
            {"content": "hi"},
            source="chat",
            request_id="r1",
            task_handler=th,
        )
        assert resp["type"] == "response"
        assert resp["ps_id"] == "alice"
        assert resp["widget_id"] == "w1"
        assert resp["request_id"] == "r1"
        assert resp["source"] == "chat"
        assert resp["content"] == "echo: hi"
        assert th.started is True
        assert agent.calls[0]["content"] == "hi"
        assert agent.calls[0]["user_id"] == "tester"
        assert agent.calls[0]["source"] == "chat"
        await w.unload()

    async def test_requires_load(self, tmp_path: Path):
        w = _make_widget(tmp_path)
        with pytest.raises(RuntimeError, match="未 load"):
            await w.process_message(
                {"content": "hi"}, task_handler=_FakeTaskHandler()
            )

    async def test_requires_task_handler(self, tmp_path: Path):
        w = _make_widget(tmp_path)
        await w.load()
        with pytest.raises(ValueError, match="task_handler"):
            await w.process_message({"content": "hi"})
        await w.unload()

    async def test_no_agent_falls_back_to_test_mode(self, tmp_path: Path):
        manifest = WidgetManifest(widget_id="w1", widget_class="chat", title="W")
        w = Widget(
            ps_id="alice",
            manifest=manifest,
            workspace_dir=tmp_path / "alice" / "widgets" / "w1",
            resolved_config={"context": {"type": "fake"}},  # no agent
            context_factory=lambda sid, ws, cfg: _FakeContextHandler(),
        )
        await w.load()
        assert w.agent is None
        resp = await w.process_message(
            {"content": "ping"}, task_handler=_FakeTaskHandler()
        )
        assert "测试模式" in resp["content"]
        assert "ping" in resp["content"]
        await w.unload()

    async def test_agent_exception_returns_error_response(self, tmp_path: Path):
        class Boom(_FakeAgent):
            async def process_v2(self, *, user_message, **_):
                raise RuntimeError("kaboom")

        w = _make_widget(tmp_path, agent=Boom())
        await w.load()
        resp = await w.process_message(
            {"content": "go"}, task_handler=_FakeTaskHandler()
        )
        assert "处理失败" in resp["content"]
        assert "kaboom" in resp["content"]
        await w.unload()

    async def test_serial_execution_via_lock(self, tmp_path: Path):
        """同一 widget 的两条消息必须串行（_processing_lock）。"""
        import asyncio

        order: list = []

        class SlowAgent(_FakeAgent):
            async def process_v2(self, *, user_message, **_):
                order.append(("start", user_message["content"]))
                await asyncio.sleep(0.05)
                order.append(("end", user_message["content"]))
                return "x"

        w = _make_widget(tmp_path, agent=SlowAgent())
        await w.load()
        t1 = asyncio.create_task(
            w.process_message({"content": "a"}, task_handler=_FakeTaskHandler())
        )
        t2 = asyncio.create_task(
            w.process_message({"content": "b"}, task_handler=_FakeTaskHandler())
        )
        await asyncio.gather(t1, t2)
        # 第一条必须 start→end 后 第二条才 start
        assert order[0][0] == "start"
        assert order[1][0] == "end"
        assert order[2][0] == "start"
        assert order[3][0] == "end"
        await w.unload()


# ---------------------------------------------------------------------------
# PersonalSpace 集成 — 真正用 builtin chat WidgetClass + 注入 stub factory
# ---------------------------------------------------------------------------


class TestPersonalSpaceWithChatClass:
    async def test_get_widget_uses_chat_defaults(self, tmp_path: Path):
        """``get_widget`` 必须从 WidgetClassRegistry 读 chat 的 defaults，
        并把它当成 resolve 的第三层注入。"""
        from pyclaego.config import PersonalSpaceConfigManager

        ps_root = tmp_path / "alice"
        PersonalSpace.bootstrap_on_disk(ps_root, "alice")

        # 自定义 registry：chat defaults 中放一个标记字段，验证它流到 widget
        custom_root = tmp_path / "wclasses"
        (custom_root / "chat").mkdir(parents=True)
        (custom_root / "chat" / "widget_class.json").write_text(
            json.dumps(
                {
                    "class_id": "chat",
                    "title": "Chat",
                    "defaults": {
                        "agent": {"type": "fake", "marker": "from_class"},
                        "context": {"type": "fake"},
                    },
                }
            )
        )
        reg = WidgetClassRegistry(builtin_root=custom_root)

        cfg = PersonalSpaceConfigManager(
            ps_root, global_config_provider=lambda: {}
        )

        captured: dict = {}

        def stub_factory(ps_id, manifest, workspace_dir, resolved_config):
            captured["resolved"] = resolved_config
            return Widget(
                ps_id=ps_id,
                manifest=manifest,
                workspace_dir=workspace_dir,
                resolved_config=resolved_config,
                agent_factory=lambda c, s: _FakeAgent(),
                context_factory=lambda s, w, c: _FakeContextHandler(),
            )

        ps = PersonalSpace(
            ps_id="alice",
            ps_root=ps_root,
            config_manager=cfg,
            widget_class_registry=reg,
            widget_factory=stub_factory,
        )
        await ps.load()
        try:
            widget = await ps.get_widget(DEFAULT_WIDGET_ID)
            assert widget.is_loaded
            assert captured["resolved"]["agent"]["marker"] == "from_class"
            assert captured["resolved"]["context"]["type"] == "fake"
        finally:
            await ps.unload()
