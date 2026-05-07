"""Phase 2.1 — PSGateway 路由测试（不接 WebSocket）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pyclaego.core import R_ACK, R_ERROR, R_REPLY, T_CHAT, T_CLOSE, T_OPEN, PSGateway
from pyclaego.personal_space import (
    DEFAULT_WIDGET_ID,
    PersonalSpaceManager,
    Widget,
    WidgetClassRegistry,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 共用 fakes
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, reply: str = "ok"):
        self.reply = reply

    async def process_v2(self, *, user_message, context_handler, session_task_handler, **_):
        return f"{self.reply}: {user_message['content']}"


class _FakeContext:
    def set_session_task_handler(self, h): ...
    def close(self): ...


class _FakeTaskManager:
    """最小 TaskManager 替身：只满足 PSGateway 需要的 3 个 async 方法。"""

    def __init__(self):
        self.created: list[dict[str, Any]] = []
        self.completed: list[str] = []
        self.failed: list[tuple] = []
        self._counter = 0

    async def create_task(self, **kwargs) -> str:
        self._counter += 1
        tid = f"t_{self._counter}"
        self.created.append({"task_id": tid, **kwargs})
        return tid

    async def complete_task(self, task_id, result=None):
        self.completed.append(task_id)

    async def fail_task(self, task_id, error):
        self.failed.append((task_id, error))


class _Recorder:
    """publish_fn 替身：把所有出站消息收进 list。"""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, conn_id: str, msg: dict[str, Any]) -> None:
        self.sent.append({"conn_id": conn_id, **msg})

    def by_type(self, t: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == t]


# ---------------------------------------------------------------------------
# fixture：构造一个完整可用的 PSManager + Gateway
# ---------------------------------------------------------------------------


def _stub_widget_factory(ps_id, manifest, workspace_dir, resolved_config):
    return Widget(
        ps_id=ps_id,
        manifest=manifest,
        workspace_dir=workspace_dir,
        resolved_config=resolved_config,
        agent_factory=lambda c, s: _FakeAgent(),
        context_factory=lambda s, w, c: _FakeContext(),
    )


@pytest.fixture
async def gateway(tmp_path: Path):
    PersonalSpaceManager.reset_instance()
    root = tmp_path / "ps"
    root.mkdir()
    # 全局配置注入 agent / context（让 widget.load 不报"缺 context"）
    psm = PersonalSpaceManager(
        root_path=root,
        max_active=4,
        global_config_provider=lambda: {
            "agent": {"type": "fake"},
            "context": {"type": "fake"},
        },
        widget_class_registry=WidgetClassRegistry(builtin_root=root / "_no"),
        widget_factory=_stub_widget_factory,
    )
    rec = _Recorder()
    tm = _FakeTaskManager()
    gw = PSGateway(psm, rec, task_manager=tm)
    yield gw, rec, tm, psm
    await gw.shutdown()
    await psm.shutdown()
    PersonalSpaceManager.reset_instance()


# ---------------------------------------------------------------------------
# 协议测试
# ---------------------------------------------------------------------------


class TestOpenClose:
    async def test_open_acks_and_lists_widgets(self, gateway):
        gw, rec, tm, psm = gateway
        await gw.handle_inbound("c1", {
            "type": T_OPEN, "request_id": "r1", "ps_id": "alice",
        })
        acks = rec.by_type(R_ACK)
        assert len(acks) == 1
        assert acks[0]["ps_id"] == "alice"
        assert DEFAULT_WIDGET_ID in acks[0]["widget_ids"]
        assert "alice" in gw.list_connections()["c1"]
        assert psm.is_loaded("alice")

    async def test_close_releases_ps(self, gateway):
        gw, rec, tm, psm = gateway
        await gw.handle_inbound("c1", {
            "type": T_OPEN, "request_id": "r1", "ps_id": "alice",
        })
        ps = await psm.get("alice")
        assert ps.active_connection_count == 1

        await gw.handle_inbound("c1", {
            "type": T_CLOSE, "request_id": "r2", "ps_id": "alice",
        })
        assert ps.active_connection_count == 0
        assert "alice" not in gw.list_connections()["c1"]

    async def test_open_missing_ps_id(self, gateway):
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {"type": T_OPEN, "request_id": "r"})
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "bad_request"

    async def test_open_invalid_ps_id(self, gateway):
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {
            "type": T_OPEN, "request_id": "r", "ps_id": "../etc",
        })
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "bad_ps_id"

    async def test_unregister_releases_all_ps(self, gateway):
        gw, rec, tm, psm = gateway
        await gw.handle_inbound("c1", {"type": T_OPEN, "request_id": "r1", "ps_id": "alice"})
        await gw.handle_inbound("c1", {"type": T_OPEN, "request_id": "r2", "ps_id": "bob"})
        assert gw.list_connections()["c1"] == {"alice", "bob"}
        await gw.unregister_connection("c1")
        assert "c1" not in gw.list_connections()
        assert (await psm.get("alice")).active_connection_count == 0


class TestChat:
    async def _drain(self):
        # 让 fire-and-forget 后台 task 跑完
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0.05)

    async def test_chat_returns_reply(self, gateway):
        gw, rec, tm, psm = gateway
        await gw.handle_inbound("c1", {
            "type": T_CHAT, "request_id": "r1",
            "ps_id": "alice", "widget_id": DEFAULT_WIDGET_ID,
            "content": "hi", "user_id": "u1",
        })
        # 立即应有 ack
        acks = rec.by_type(R_ACK)
        assert acks and acks[0]["action"] == T_CHAT

        # 等待 reply
        for _ in range(20):
            await asyncio.sleep(0.02)
            replies = rec.by_type(R_REPLY)
            if replies:
                break
        replies = rec.by_type(R_REPLY)
        assert replies, f"never got reply, sent={rec.sent}"
        r = replies[0]
        assert r["ps_id"] == "alice"
        assert r["widget_id"] == DEFAULT_WIDGET_ID
        assert r["request_id"] == "r1"
        assert "ok: hi" in r["content"]

        # TaskManager 调用记录
        assert len(tm.created) == 1
        assert tm.created[0]["user_id"] == "u1"
        assert tm.completed == [tm.created[0]["task_id"]]
        # PS in_flight 应已归零
        assert (await psm.get("alice")).in_flight_tasks == 0

    async def test_chat_auto_opens_ps(self, gateway):
        gw, rec, tm, psm = gateway
        # 不先 open，直接 chat
        await gw.handle_inbound("c1", {
            "type": T_CHAT, "request_id": "r1",
            "ps_id": "alice", "widget_id": DEFAULT_WIDGET_ID,
            "content": "hi",
        })
        for _ in range(20):
            await asyncio.sleep(0.02)
            if rec.by_type(R_REPLY):
                break
        assert rec.by_type(R_REPLY)
        assert "alice" in gw.list_connections()["c1"]

    async def test_chat_missing_widget_id(self, gateway):
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {
            "type": T_CHAT, "request_id": "r", "ps_id": "alice",
        })
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "bad_request"

    async def test_chat_unknown_widget(self, gateway):
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {
            "type": T_CHAT, "request_id": "r",
            "ps_id": "alice", "widget_id": "nope",
            "content": "hi",
        })
        for _ in range(20):
            await asyncio.sleep(0.02)
            if rec.by_type(R_ERROR):
                break
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "widget_not_found"

    async def test_chat_widget_exception_marks_task_failed(self, gateway):
        gw, rec, tm, psm = gateway

        # 先打开 PS，再把 widget 替换成会抛错的 agent
        await gw.handle_inbound("c1", {"type": T_OPEN, "request_id": "r0", "ps_id": "alice"})
        ps = await psm.get("alice")
        widget = await ps.get_widget(DEFAULT_WIDGET_ID)

        class Boom:
            async def process_v2(self, **_):
                raise RuntimeError("boom")
        widget.agent = Boom()

        await gw.handle_inbound("c1", {
            "type": T_CHAT, "request_id": "r1",
            "ps_id": "alice", "widget_id": DEFAULT_WIDGET_ID,
            "content": "hi",
        })
        # widget 自己捕获了 agent 异常并返回 "处理失败" reply（不算 widget_error）
        for _ in range(20):
            await asyncio.sleep(0.02)
            if rec.by_type(R_REPLY):
                break
        replies = rec.by_type(R_REPLY)
        assert replies and "处理失败" in replies[0]["content"]
        # task 应被 complete（widget 没把异常往上抛）
        assert tm.completed and not tm.failed


class TestUnknownAndControl:
    async def test_unknown_type(self, gateway):
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {"type": "weird", "request_id": "r"})
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "unknown_type"

    async def test_control_stop_missing_ids(self, gateway):
        """control/stop 无 ps_id/widget_id 应返回 bad_request"""
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {"type": "control", "action": "stop", "request_id": "r"})
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "bad_request"

    async def test_control_unknown_action(self, gateway):
        """control 未知 action 应返回 unknown_action"""
        gw, rec, *_ = gateway
        await gw.handle_inbound("c1", {
            "type": "control", "action": "reboot",
            "ps_id": "alice", "widget_id": "w", "request_id": "r",
        })
        errs = rec.by_type(R_ERROR)
        assert errs and errs[0]["code"] == "unknown_action"
