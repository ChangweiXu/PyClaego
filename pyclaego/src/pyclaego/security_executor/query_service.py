"""QueryService — per-session query queue for interactive user confirmation.

The widget's ``_processing_lock`` is already held by the agent loop while the
agent is running a tool call.  ``QueryService`` exploits this: it enqueues a
``PendingQuery`` whose ``Future`` is awaited *inside* that lock, while the
``PSGateway`` message handler picks up new inbound chat messages, routes them
through ``try_resolve``, and resolves the future — unblocking the waiting agent.

Key design decisions:
- Keyed by ``session_id`` (= ``TaskBelonging.key()``).
- Broadcast is delegated to an injected async callback (set by PSGateway).
- ``session_id`` encodes ``ps_id`` as its first ``__``-delimited segment, so we
  can derive ``ps_id`` without extra coupling.
- Queue is a FIFO deque; only the *head* query is resolvable.
- ``/stop`` arrives via a control frame; ``PSGateway._handle_control`` calls
  ``clear_all``, which cancels every future in the queue.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..logging import get_running_log

_rlog = get_running_log()

# Sentinel returned inside the future when the queue is forcibly cancelled.
CANCEL_SENTINEL = "__cancelled__"

# MVP: queue capacity disabled (kept as 0 = unlimited).
# TODO: re-enable with "reject newest" policy once back-pressure UX is designed.
_DEFAULT_MAX_QUEUE = 0


class ResolveKind(Enum):
    ACCEPTED = "accepted"   # user picked a valid choice
    REJECTED = "rejected"   # text did not match any choice
    STOPPED  = "stopped"    # /stop received
    PASS_THROUGH = "pass_through"  # no pending query, caller should process normally


@dataclass
class Choice:
    value: str
    label: str
    description: str = ""


@dataclass
class PendingQuery:
    """One pending confirmation request.

    ``future`` is resolved with the chosen ``value`` string, or with
    ``CANCEL_SENTINEL`` on cancellation.
    """
    query_id: str
    session_id: str
    origin: str                   # "rule" | "tool"
    tool_name: str | None
    tool_args: dict[str, Any] | None
    prompt: str
    choices: list[Choice]
    deny_values: list[str]        # resolved → execute; in here → block
    default: str | None        # value used when timeout fires
    timeout_s: int | None
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    # future is intentionally left unset at construction time; enqueue() always
    # binds it to the currently-running event loop via get_running_loop().
    future: asyncio.Future = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "session_id": self.session_id,
            "origin": self.origin,
            "tool_name": self.tool_name,
            "prompt": self.prompt,
            "choices": [{"value": c.value, "label": c.label, "description": c.description}
                        for c in self.choices],
            "deny_values": self.deny_values,
            "default": self.default,
            "timeout_s": self.timeout_s,
            "created_at": self.created_at,
        }


@dataclass
class ResolveOutcome:
    kind: ResolveKind
    query_id: str | None = None
    value: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]
"""async (ps_id, payload) -> None"""


class QueryService:
    """Singleton service that manages per-session query queues.

    Usage::

        qs = QueryService.get_instance()

        # PSGateway registers its broadcast callback once at startup:
        qs.set_broadcast_fn(gateway.publish_to_ps)

        # SecurityHandler suspends here:
        query_id = await qs.enqueue(pending)
        choice = await qs.wait_resolved(query_id)

        # PSGateway routes incoming chat messages:
        outcome = await qs.try_resolve(session_id, text)
    """

    _instance: QueryService | None = None

    def __new__(cls) -> QueryService:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        # session_id → deque[PendingQuery]
        self._queues: dict[str, deque[PendingQuery]] = {}
        # query_id → PendingQuery  (fast lookup for wait_resolved)
        self._by_id: dict[str, PendingQuery] = {}
        self._broadcast_fn: BroadcastFn | None = None
        self._max_queue: int = _DEFAULT_MAX_QUEUE
        self._initialized = True
        _rlog.info("core_service", "[QueryService] Initialized")

    @classmethod
    def get_instance(cls) -> QueryService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_broadcast_fn(self, fn: BroadcastFn) -> None:
        """Inject the PSGateway broadcast callback."""
        self._broadcast_fn = fn

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    async def enqueue(self, query: PendingQuery) -> str:
        """Add a query to the tail of the session's queue.

        **Lazy Broadcast**: only the HEAD query broadcasts ``query.opened``
        to the frontend (it owns the single PendingQuery UI slot). Non-HEAD
        queries broadcast a lightweight ``query.queued`` event carrying only
        ``queue_depth`` for the badge counter.

        Returns ``query_id``.
        """
        sid = query.session_id
        # Create the future on the loop that is currently running so that
        # the awaiting coroutine and the resolver live on the same loop.
        query.future = asyncio.get_running_loop().create_future()
        q = self._queues.setdefault(sid, deque())

        # MVP: no queue cap. (_max_queue == 0 means unlimited)
        if self._max_queue > 0 and len(q) >= self._max_queue:
            _rlog.warning(
                "core_service",
                f"[QueryService] session={sid} queue full ({self._max_queue}), "
                "discarding oldest",
            )
            oldest = q.popleft()
            self._by_id.pop(oldest.query_id, None)
            if not oldest.future.done():
                oldest.future.set_result(CANCEL_SENTINEL)

        q.append(query)
        self._by_id[query.query_id] = query
        is_head = len(q) == 1

        _rlog.info(
            "core_service",
            f"[QueryService] enqueued query_id={query.query_id} "
            f"session={sid} tool={query.tool_name} "
            f"is_head={is_head} depth={len(q)}",
        )

        if is_head:
            await self._broadcast_opened(query, queue_depth=len(q))
        else:
            await self._broadcast(sid, {
                "type": "event",
                "event": "query.queued",
                "query_id": query.query_id,
                "session_id": sid,
                "origin": query.origin,
                "tool_name": query.tool_name,
                "queue_depth": len(q),
                "timestamp": datetime.now().isoformat(),
            })

        # MVP: timeout disabled. ``timeout_s`` is preserved on PendingQuery and
        # forwarded to the frontend, but no timer is registered. Re-enable once
        # we move to a fair "start countdown when promoted to HEAD" model.
        # if query.timeout_s and query.timeout_s > 0:
        #     asyncio.get_event_loop().call_later(
        #         query.timeout_s, self._on_timeout, query.query_id,
        #     )

        return query.query_id

    async def _broadcast_opened(
        self, query: PendingQuery, queue_depth: int,
    ) -> None:
        """Broadcast ``query.opened`` for the current HEAD."""
        await self._broadcast(query.session_id, {
            "type": "event",
            "event": "query.opened",
            "query_id": query.query_id,
            "session_id": query.session_id,
            "origin": query.origin,
            "tool_name": query.tool_name,
            "prompt": query.prompt,
            "choices": [{"value": c.value, "label": c.label, "description": c.description}
                        for c in query.choices],
            "default": query.default,
            "timeout_s": query.timeout_s,
            "queue_depth": queue_depth,
            "timestamp": datetime.now().isoformat(),
        })

    async def _promote_next_head(self, session_id: str) -> None:
        """After HEAD is dequeued, broadcast ``query.opened`` for the new HEAD
        (if any). Safe to call when queue is already empty.
        """
        q = self._queues.get(session_id)
        if not q:
            return
        new_head = q[0]
        _rlog.info(
            "core_service",
            f"[QueryService] promoting query_id={new_head.query_id} "
            f"session={session_id} to HEAD (depth={len(q)})",
        )
        await self._broadcast_opened(new_head, queue_depth=len(q))

    def _on_timeout(self, query_id: str) -> None:
        """Timeout handler. Currently unused (MVP disables timeout in
        ``enqueue``). Kept as a placeholder so the logic is ready when the
        fair "countdown-on-promote" model is implemented.
        """
        q = self._by_id.get(query_id)
        if q is None or q.future.done():
            return
        default = q.default
        sid = q.session_id
        if default is not None:
            _rlog.info(
                "core_service",
                f"[QueryService] query_id={query_id} timed out, "
                f"auto-resolving with default={default!r}",
            )
            q.future.set_result(default)
            self._dequeue_head(sid, query_id)
            asyncio.ensure_future(self._broadcast(sid, {
                "type": "event",
                "event": "query.resolved",
                "query_id": query_id,
                "session_id": sid,
                "value": default,
                "by": "timeout",
                "timestamp": datetime.now().isoformat(),
            }))
        else:
            _rlog.info(
                "core_service",
                f"[QueryService] query_id={query_id} timed out with no default, cancelling",
            )
            q.future.set_result(CANCEL_SENTINEL)
            self._dequeue_head(sid, query_id)
        # Promote the new HEAD so the frontend reflects the next pending query.
        asyncio.ensure_future(self._promote_next_head(sid))

    # ------------------------------------------------------------------
    # Wait
    # ------------------------------------------------------------------

    async def wait_resolved(self, query_id: str) -> str:
        """Await the future for the given query and return the chosen value.

        Returns ``CANCEL_SENTINEL`` if cancelled.
        """
        q = self._by_id.get(query_id)
        if q is None:
            return CANCEL_SENTINEL
        result: str = await q.future
        # Cleanup after resolution
        self._by_id.pop(query_id, None)
        return result

    # ------------------------------------------------------------------
    # Resolve (called from PSGateway on inbound chat)
    # ------------------------------------------------------------------

    async def try_resolve(self, session_id: str, text: str) -> ResolveOutcome:
        """Try to consume ``text`` as a reply to the head query.

        Returns a :class:`ResolveOutcome` indicating what happened.  The caller
        must inspect the ``kind`` field:

        - ``PASS_THROUGH``: no pending query, route normally.
        - ``ACCEPTED``: choice matched; caller should *not* route to widget.
        - ``REJECTED``: bad input; caller should push a ``query.rejected`` event
          and *not* route to widget.
        - ``STOPPED``: ``/stop`` processed; queue + task cancelled.
        """
        q = self._queues.get(session_id)
        if not q:
            return ResolveOutcome(ResolveKind.PASS_THROUGH)

        text = text.strip()

        # /stop — forcibly clear
        if text == "/stop":
            await self.clear_all(session_id, reason="user_stop")
            return ResolveOutcome(ResolveKind.STOPPED)

        head = q[0]

        # Try to match against choices
        value = self._match_choice(text, head.choices)
        if value is None:
            labels = ", ".join(
                f"{i+1}/{c.value}/{c.label}" for i, c in enumerate(head.choices)
            )
            return ResolveOutcome(
                ResolveKind.REJECTED,
                query_id=head.query_id,
                reason=f"Not a valid choice. Expected one of: {labels}",
            )

        # Valid choice — resolve and dequeue
        head.future.set_result(value)
        self._dequeue_head(session_id, head.query_id)
        _rlog.info(
            "core_service",
            f"[QueryService] query_id={head.query_id} resolved with value={value!r}",
        )
        await self._broadcast(session_id, {
            "type": "event",
            "event": "query.resolved",
            "query_id": head.query_id,
            "session_id": session_id,
            "value": value,
            "by": "user",
            "timestamp": datetime.now().isoformat(),
        })
        # Promote next pending query (if any) to HEAD so the frontend updates.
        await self._promote_next_head(session_id)
        return ResolveOutcome(ResolveKind.ACCEPTED, query_id=head.query_id, value=value)

    @staticmethod
    def _match_choice(text: str, choices: list[Choice]) -> str | None:
        """Return the matched choice ``value``, or ``None``."""
        text_lower = text.lower()
        for i, c in enumerate(choices):
            if (
                text_lower == c.value.lower()
                or text_lower == c.label.lower()
                or text == str(i + 1)
            ):
                return c.value
        return None

    # ------------------------------------------------------------------
    # Clear / cancel
    # ------------------------------------------------------------------

    async def clear_all(self, session_id: str, reason: str = "cancelled") -> None:
        """Resolve all pending queries in the session with ``CANCEL_SENTINEL``."""
        q = self._queues.pop(session_id, None)
        if not q:
            return
        cleared_count = len(q)
        for pending in q:
            # Leave entry in _by_id so that any awaiting wait_resolved() can
            # still read the CANCEL_SENTINEL from the resolved future.
            # wait_resolved() removes the entry from _by_id after it returns.
            if not pending.future.done():
                pending.future.set_result(CANCEL_SENTINEL)
        _rlog.info(
            "core_service",
            f"[QueryService] cleared {cleared_count} queries for session={session_id} "
            f"reason={reason}",
        )
        await self._broadcast(session_id, {
            "type": "event",
            "event": "query.cleared",
            "session_id": session_id,
            "reason": reason,
            "cleared_count": cleared_count,
            "timestamp": datetime.now().isoformat(),
        })

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def has_pending(self, session_id: str) -> bool:
        """Return True if the session has at least one pending query."""
        return bool(self._queues.get(session_id))

    def get_pending(self, session_id: str) -> list[dict[str, Any]]:
        """Return serialized list of pending queries for the session."""
        return [q.to_dict() for q in self._queues.get(session_id, [])]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dequeue_head(self, session_id: str, query_id: str) -> None:
        """Remove the head entry from the session queue.

        ``_by_id`` is intentionally NOT cleaned up here so that a concurrently
        awaiting ``wait_resolved`` can still find and read the future's result.
        ``wait_resolved`` is responsible for removing the entry from ``_by_id``.
        """
        q = self._queues.get(session_id)
        if not q:
            return
        if q[0].query_id == query_id:
            q.popleft()
        if not q:
            del self._queues[session_id]
        # NOTE: do NOT pop from _by_id here — wait_resolved does that.

    async def _broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        if self._broadcast_fn is None:
            return
        parts = session_id.split("__", 1)
        ps_id = parts[0]
        widget_id = parts[1] if len(parts) > 1 else ""
        # Inject routing fields so the dashboard bridge can target the right widget
        enriched = {**payload, "ps_id": ps_id, "widget_id": widget_id}
        try:
            await self._broadcast_fn(ps_id, enriched)
        except Exception as exc:
            _rlog.warning(
                "core_service",
                f"[QueryService] broadcast failed for session={session_id}: {exc}",
            )


def get_query_service() -> QueryService:
    return QueryService.get_instance()


__all__ = [
    "CANCEL_SENTINEL",
    "Choice",
    "PendingQuery",
    "QueryService",
    "ResolveKind",
    "ResolveOutcome",
    "get_query_service",
]
