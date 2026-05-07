"""TaskBelonging / WidgetTaskHandler 基础单元测试

覆盖 PersonalSpace 模型迁移 Phase 0.1 引入的新类型：
- TaskBelonging.key/widget_key/with_subagent/without_subagent
- TaskBelonging.to_dict/from_dict 往返
- Task 同时接受 session_id 与 belongs_to，并保持双向同步
- TaskEvent 的 belongs_to 默认从 session_id 派生
- WidgetTaskHandler.derive_for_subagent 不可变派生
"""

from datetime import datetime

import pytest

from pyclaego.task_manager import (
    EventType,
    Task,
    TaskBelonging,
    TaskEvent,
    WidgetTaskHandler,
    generate_task_id,
)
from pyclaego.task_manager.base import TaskStatus, TaskType

# ---------------------------------------------------------------------------
# TaskBelonging
# ---------------------------------------------------------------------------


class TestTaskBelonging:
    def test_key_ps_only(self):
        b = TaskBelonging(ps_id="alice")
        assert b.key() == "alice"
        assert b.widget_key() == "alice"

    def test_key_with_widget(self):
        b = TaskBelonging(ps_id="alice", widget_id="w_papers")
        assert b.key() == "alice__w_papers"
        assert b.widget_key() == "alice__w_papers"

    def test_key_with_subagent(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1", subagent_id="sa_99")
        assert b.key() == "alice__w1__sa_99"
        assert b.widget_key() == "alice__w1"

    def test_subagent_requires_widget(self):
        with pytest.raises(ValueError):
            TaskBelonging(ps_id="alice", subagent_id="sa_99")

    def test_with_subagent_immutable(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1")
        b2 = b.with_subagent("sa_1")
        assert b.subagent_id is None
        assert b2.subagent_id == "sa_1"
        assert b2.ps_id == "alice" and b2.widget_id == "w1"

    def test_without_subagent(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1", subagent_id="sa_1")
        b2 = b.without_subagent()
        assert b2.subagent_id is None
        assert b2.widget_id == "w1"

    def test_serialize_roundtrip(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1", subagent_id="sa_1")
        d = b.to_dict()
        assert d == {"ps_id": "alice", "widget_id": "w1", "subagent_id": "sa_1"}
        assert TaskBelonging.from_dict(d) == b

    def test_from_dict_none(self):
        assert TaskBelonging.from_dict(None) is None
        assert TaskBelonging.from_dict({}) is None


# ---------------------------------------------------------------------------
# Task / TaskEvent  兼容性
# ---------------------------------------------------------------------------


def _make_task(**overrides):
    defaults = dict(
        task_id="t_x",
        session_id="alice",
        task_type=TaskType.AGENT_LOOP,
        name="root",
        status=TaskStatus.PENDING,
        created_at=datetime.now(),
    )
    defaults.update(overrides)
    return Task(**defaults)


class TestTaskCompat:
    def test_session_id_only_derives_belonging(self):
        t = _make_task()
        assert t.belongs_to == TaskBelonging(ps_id="alice")
        assert t.session_id == "alice"

    def test_belonging_overrides_session_id(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1")
        t = _make_task(session_id="ignored", belongs_to=b)
        assert t.belongs_to is b
        assert t.session_id == "alice__w1"

    def test_to_dict_contains_belongs_to(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1")
        t = _make_task(belongs_to=b)
        d = t.to_dict()
        assert d["session_id"] == "alice__w1"
        assert d["belongs_to"] == {"ps_id": "alice", "widget_id": "w1", "subagent_id": None}


class TestTaskEventCompat:
    def test_default_belonging_from_session_id(self):
        e = TaskEvent(
            event_type=EventType.TASK_CREATED,
            session_id="alice",
            task_id="t",
            timestamp=datetime.now(),
            task_snapshot={},
        )
        assert e.belongs_to == TaskBelonging(ps_id="alice")

    def test_explicit_belonging_overrides_session_id(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1")
        e = TaskEvent(
            event_type=EventType.WIDGET_CREATED,
            session_id="ignored",
            task_id="t",
            timestamp=datetime.now(),
            task_snapshot={},
            belongs_to=b,
        )
        assert e.session_id == "alice__w1"
        assert e.belongs_to is b


class TestEventTypeAliases:
    def test_session_aliases_are_widget(self):
        assert EventType.SESSION_CREATED is EventType.WIDGET_CREATED
        assert EventType.SESSION_COMPLETED is EventType.WIDGET_COMPLETED
        assert EventType.SESSION_FAILED is EventType.WIDGET_FAILED
        assert EventType.SESSION_CANCELLED is EventType.WIDGET_CANCELLED


# ---------------------------------------------------------------------------
# WidgetTaskHandler.derive_for_subagent
# ---------------------------------------------------------------------------


class TestTaskHandlerDerive:
    def test_from_belonging_carries_subagent(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1", subagent_id="sa_init")
        h = WidgetTaskHandler.from_belonging(b, user_id="u1", task_id="t1")
        assert h.belongs_to == b
        assert h.get_subagent_id() == "sa_init"

    def test_derive_is_immutable(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1")
        h = WidgetTaskHandler.from_belonging(b, user_id="u1", task_id="t1")
        sa = h.derive_for_subagent("sa_99")
        # 原 handler 不变
        assert h.belongs_to.subagent_id is None
        assert h.get_subagent_id() is None
        # 派生 handler 共享 task_id, user_id；只多一个 subagent
        assert sa.belongs_to.subagent_id == "sa_99"
        assert sa.belongs_to.widget_id == "w1"
        assert sa.get_subagent_id() == "sa_99"
        assert sa._task_id == h._task_id  # 同一任务节点
        assert sa._user_id == h._user_id


# ---------------------------------------------------------------------------
# generate_task_id
# ---------------------------------------------------------------------------


class TestGenerateTaskId:
    def test_str_input_compat(self):
        tid = generate_task_id("alice")
        assert tid.startswith("alice-")

    def test_belonging_input_uses_key(self):
        b = TaskBelonging(ps_id="alice", widget_id="w1")
        tid = generate_task_id(b)
        assert tid.startswith("alice__w1-")
