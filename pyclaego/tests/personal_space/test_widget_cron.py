"""Tests for WidgetCronScheduler (Phase 9 / 2.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyclaego.personal_space.cron import (
    WidgetCronScheduler,
    WidgetCronTrigger,
    render_prompt,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# WidgetCronTrigger.from_dict
# ---------------------------------------------------------------------------


class TestTrigger:
    async def test_from_dict_with_schedule(self):
        t = WidgetCronTrigger.from_dict(
            {"id": "morn", "schedule": "0 8 * * *", "prompt": "hi"},
            fallback_id="cr_00",
        )
        assert t.id == "morn"
        assert t.schedule == "0 8 * * *"
        assert t.interval_seconds is None
        assert t.enabled is True
        assert t.user_id == "cron"

    async def test_from_dict_with_interval(self):
        t = WidgetCronTrigger.from_dict(
            {"interval_seconds": 30, "prompt": "tick"},
            fallback_id="cr_03",
        )
        assert t.id == "cr_03"
        assert t.interval_seconds == 30
        assert t.schedule is None

    async def test_from_dict_missing_prompt(self):
        with pytest.raises(ValueError, match="prompt"):
            WidgetCronTrigger.from_dict(
                {"schedule": "* * * * *"}, fallback_id="x"
            )

    async def test_from_dict_missing_schedule_and_interval(self):
        with pytest.raises(ValueError, match="schedule"):
            WidgetCronTrigger.from_dict(
                {"prompt": "hi"}, fallback_id="x"
            )


# ---------------------------------------------------------------------------
# render_prompt
# ---------------------------------------------------------------------------


class TestTemplate:
    async def test_substitutes_user_params(self):
        out = render_prompt("hello {name}", {"name": "world"})
        assert out == "hello world"

    async def test_keeps_unknown_placeholders(self):
        out = render_prompt("hi {unknown_var}", {})
        assert "{unknown_var}" in out

    async def test_injects_now_today(self):
        out = render_prompt("date={today} year={year}", {})
        # 至少 today 不为空
        assert "date=" in out and "year=" in out
        assert "{today}" not in out


# ---------------------------------------------------------------------------
# Scheduler scan + fire（不实际启动 APScheduler 计时；用 _fire 直接验证投递）
# ---------------------------------------------------------------------------


def _write_widget(ps_root: Path, ps_id: str, widget_id: str, cron: list[dict]) -> None:
    wdir = ps_root / ps_id / "widgets" / widget_id
    wdir.mkdir(parents=True)
    (ps_root / ps_id / "widgets" / widget_id / "widget.json").write_text(
        json.dumps({
            "widget_id": widget_id,
            "widget_class": "chat",
            "title": widget_id,
            "cron": cron,
        })
    )


class TestScannerAndFire:
    async def test_scan_picks_up_triggers(self, tmp_path: Path):
        ps_root = tmp_path / "ps"
        ps_root.mkdir()
        _write_widget(ps_root, "alice", "w1", [
            {"id": "t1", "schedule": "0 9 * * *", "prompt": "morning"},
            {"id": "t2", "interval_seconds": 60, "prompt": "tick"},
        ])
        _write_widget(ps_root, "bob", "w2", [
            {"id": "t3", "interval_seconds": 5, "prompt": "p", "enabled": False},
        ])

        async def noop(conn, msg):
            pass

        sched = WidgetCronScheduler(ps_root=ps_root, handle_inbound=noop)
        sched.scan_and_register()
        jobs = sched.list_jobs()
        ids = sorted((j["ps_id"], j["widget_id"], j["trigger_id"]) for j in jobs)
        assert ids == [("alice", "w1", "t1"), ("alice", "w1", "t2")]
        # disabled 的 t3 不应被注册
        assert all(j["trigger_id"] != "t3" for j in jobs)
        sched.shutdown()

    async def test_fire_publishes_open_chat_close(self, tmp_path: Path):
        ps_root = tmp_path / "ps"
        ps_root.mkdir()

        captured: list[dict[str, Any]] = []

        async def fake_inbound(conn_id: str, msg: dict[str, Any]) -> None:
            captured.append({"conn": conn_id, **msg})

        sched = WidgetCronScheduler(ps_root=ps_root, handle_inbound=fake_inbound)
        trig = WidgetCronTrigger(
            id="t1", prompt="hi {name}", schedule="* * * * *",
            params={"name": "alice"},
        )
        await sched._fire(ps_id="alice", widget_id="w1", trig=trig)

        # 三段式：open / chat / close
        types = [m["type"] for m in captured]
        assert types == ["open", "chat", "close"]

        chat_msg = captured[1]
        assert chat_msg["ps_id"] == "alice"
        assert chat_msg["widget_id"] == "w1"
        assert chat_msg["source"] == "cron"
        assert chat_msg["trigger_id"] == "t1"
        assert chat_msg["content"] == "hi alice"
        assert chat_msg["user_id"] == "cron"
        # conn_id 一致（同一次触发用同一个虚拟连接）
        conns = {m["conn"] for m in captured}
        assert len(conns) == 1
        assert next(iter(conns)).startswith("cron:")

        sched.shutdown()

    async def test_fire_sends_close_even_if_chat_raises(self, tmp_path: Path):
        ps_root = tmp_path / "ps"
        ps_root.mkdir()
        captured: list[dict[str, Any]] = []

        async def flaky(conn_id, msg):
            captured.append(msg)
            if msg["type"] == "chat":
                raise RuntimeError("boom")

        sched = WidgetCronScheduler(ps_root=ps_root, handle_inbound=flaky)
        trig = WidgetCronTrigger(id="t1", prompt="x", schedule="* * * * *")
        await sched._fire(ps_id="a", widget_id="w", trig=trig)
        types = [m["type"] for m in captured]
        assert types[0] == "open"
        assert "close" in types
        sched.shutdown()

    async def test_invalid_widget_json_skipped(self, tmp_path: Path):
        ps_root = tmp_path / "ps"
        wdir = ps_root / "alice" / "widgets" / "bad"
        wdir.mkdir(parents=True)
        (wdir / "widget.json").write_text("{not json")

        async def noop(conn, msg):
            pass

        sched = WidgetCronScheduler(ps_root=ps_root, handle_inbound=noop)
        added = sched.scan_and_register()
        assert added == 0
        sched.shutdown()
