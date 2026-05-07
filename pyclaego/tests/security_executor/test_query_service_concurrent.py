"""Tests for QueryService Lazy-Broadcast / single-active-HEAD semantics.

Verifies the fix for: multiple subagents enqueue query_user concurrently →
the frontend single-slot UI was overwritten by the LAST event, but try_resolve
matched against the HEAD → REJECTED → deadlock.

The fix: only HEAD broadcasts ``query.opened``; non-HEAD broadcasts
``query.queued`` (count-only); after HEAD is dequeued, the next HEAD is
auto-promoted with a fresh ``query.opened``.
"""
from __future__ import annotations

import asyncio

import pytest

from pyclaego.security_executor.query_service import (
    CANCEL_SENTINEL,
    Choice,
    PendingQuery,
    QueryService,
    ResolveKind,
)


def _make_pending(qid: str, sid: str, *, origin: str = "tool",
                  tool_name: str = "query_user") -> PendingQuery:
    return PendingQuery(
        query_id=qid,
        session_id=sid,
        origin=origin,
        tool_name=tool_name,
        tool_args=None,
        prompt=f"prompt-{qid}",
        choices=[Choice(value="ok", label="OK"), Choice(value="no", label="No")],
        deny_values=[],
        default=None,
        timeout_s=None,
    )


@pytest.fixture
def fresh_qs():
    """Reset the singleton between tests."""
    QueryService._instance = None  # type: ignore[attr-defined]
    qs = QueryService.get_instance()
    events: list[tuple[str, dict]] = []

    async def capture(ps_id: str, payload: dict) -> None:
        events.append((ps_id, payload))

    qs.set_broadcast_fn(capture)
    yield qs, events
    QueryService._instance = None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_head_broadcasts_opened_others_queued(fresh_qs):
    """First enqueue → query.opened; second enqueue → query.queued."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    await qs.enqueue(_make_pending("q1", sid))
    await qs.enqueue(_make_pending("q2", sid))

    types = [ev[1]["event"] for ev in events]
    assert types == ["query.opened", "query.queued"]
    assert events[0][1]["query_id"] == "q1"
    assert events[0][1]["queue_depth"] == 1
    assert events[1][1]["query_id"] == "q2"
    assert events[1][1]["queue_depth"] == 2


@pytest.mark.asyncio
async def test_resolve_promotes_next_head(fresh_qs):
    """After HEAD resolves, the next pending query is broadcast as opened."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    p1, p2 = _make_pending("q1", sid), _make_pending("q2", sid)
    await qs.enqueue(p1)
    await qs.enqueue(p2)
    events.clear()

    outcome = await qs.try_resolve(sid, "ok")
    assert outcome.kind == ResolveKind.ACCEPTED
    assert outcome.query_id == "q1"
    assert p1.future.done() and p1.future.result() == "ok"

    types = [ev[1]["event"] for ev in events]
    assert types == ["query.resolved", "query.opened"]
    assert events[0][1]["query_id"] == "q1"
    assert events[1][1]["query_id"] == "q2"
    assert events[1][1]["queue_depth"] == 1


@pytest.mark.asyncio
async def test_concurrent_enqueue_and_sequential_resolve(fresh_qs):
    """End-to-end: simulate two subagents awaiting concurrently, user answers
    in HEAD order, both futures resolve correctly."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    p1, p2 = _make_pending("q1", sid), _make_pending("q2", sid)

    async def enqueue_and_wait(p):
        await qs.enqueue(p)
        return await qs.wait_resolved(p.query_id)

    task1 = asyncio.create_task(enqueue_and_wait(p1))
    await asyncio.sleep(0)  # let task1 enqueue first
    task2 = asyncio.create_task(enqueue_and_wait(p2))
    await asyncio.sleep(0)

    # Resolve HEAD (q1) first
    out1 = await qs.try_resolve(sid, "ok")
    assert out1.kind == ResolveKind.ACCEPTED and out1.query_id == "q1"
    val1 = await task1
    assert val1 == "ok"

    # Now q2 has been promoted → resolve it
    out2 = await qs.try_resolve(sid, "no")
    assert out2.kind == ResolveKind.ACCEPTED and out2.query_id == "q2"
    val2 = await task2
    assert val2 == "no"


@pytest.mark.asyncio
async def test_rule_and_tool_queries_share_fifo(fresh_qs):
    """Rule-origin (security review) and tool-origin (query_user) share the
    same queue and promote in FIFO order."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    p_rule = _make_pending("r1", sid, origin="rule", tool_name="bash")
    p_tool = _make_pending("t1", sid, origin="tool", tool_name="query_user")
    await qs.enqueue(p_rule)
    await qs.enqueue(p_tool)

    assert events[0][1]["event"] == "query.opened"
    assert events[0][1]["origin"] == "rule"
    assert events[1][1]["event"] == "query.queued"
    assert events[1][1]["origin"] == "tool"

    events.clear()
    await qs.try_resolve(sid, "ok")
    types = [ev[1]["event"] for ev in events]
    assert types == ["query.resolved", "query.opened"]
    assert events[1][1]["origin"] == "tool"


@pytest.mark.asyncio
async def test_clear_all_cancels_all_with_count(fresh_qs):
    """clear_all cancels every pending future and reports cleared_count."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    p1, p2, p3 = (_make_pending(f"q{i}", sid) for i in range(1, 4))
    await qs.enqueue(p1)
    await qs.enqueue(p2)
    await qs.enqueue(p3)
    events.clear()

    await qs.clear_all(sid, reason="user_stop")

    assert all(p.future.done() and p.future.result() == CANCEL_SENTINEL
               for p in (p1, p2, p3))
    cleared_events = [ev for ev in events if ev[1]["event"] == "query.cleared"]
    assert len(cleared_events) == 1
    assert cleared_events[0][1]["cleared_count"] == 3
    assert cleared_events[0][1]["reason"] == "user_stop"
    # Queue is empty; no spurious query.opened broadcast after clear.
    opened_after = [ev for ev in events if ev[1]["event"] == "query.opened"]
    assert opened_after == []


@pytest.mark.asyncio
async def test_rejected_head_does_not_promote(fresh_qs):
    """Invalid input rejects head but does NOT consume it; next promote only
    happens after a valid resolve."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    await qs.enqueue(_make_pending("q1", sid))
    await qs.enqueue(_make_pending("q2", sid))
    events.clear()

    out = await qs.try_resolve(sid, "garbage-not-a-choice")
    assert out.kind == ResolveKind.REJECTED
    # No new opened/resolved broadcast on rejection
    assert events == []
    # Queue is still intact
    assert qs.has_pending(sid)
    assert len(qs.get_pending(sid)) == 2


@pytest.mark.asyncio
async def test_no_max_queue_cap_in_mvp(fresh_qs):
    """MVP disables the per-session queue cap (_max_queue=0)."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    # Enqueue more than the old cap (16) — none should be discarded.
    for i in range(25):
        await qs.enqueue(_make_pending(f"q{i}", sid))

    assert len(qs.get_pending(sid)) == 25
    # Only one query.opened (the head); the rest are query.queued.
    opened = [ev for ev in events if ev[1]["event"] == "query.opened"]
    queued = [ev for ev in events if ev[1]["event"] == "query.queued"]
    assert len(opened) == 1
    assert len(queued) == 24


@pytest.mark.asyncio
async def test_timeout_disabled_in_mvp(fresh_qs):
    """MVP: ``timeout_s`` is preserved/forwarded but no timer fires."""
    qs, events = fresh_qs
    sid = "ps1__widget1"

    p = _make_pending("q1", sid)
    p.timeout_s = 1
    p.default = "auto"
    await qs.enqueue(p)
    # timeout_s is forwarded to the frontend payload
    assert events[0][1]["timeout_s"] == 1

    # Wait longer than timeout — future must NOT be auto-resolved.
    await asyncio.sleep(1.2)
    assert not p.future.done()
