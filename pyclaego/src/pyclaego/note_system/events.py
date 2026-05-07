"""VaultEvent dataclass and lightweight asyncio EventBus for the notes widget."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from ..logging import get_running_log

_rlog = get_running_log()

VaultEventType = Literal["created", "modified", "deleted", "renamed", "index_ready"]


@dataclass
class VaultEvent:
    type: VaultEventType
    rel_path: str                            # always the current/new path
    doc_id: str | None = None
    title: str | None = None
    modified_at: int | None = None        # Unix ms
    old_path: str | None = None           # only for "renamed"
    stub: bool = False                       # True if this doc has no file on disk yet


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

AsyncListener = Callable[[VaultEvent], Coroutine[Any, Any, None]]


class EventBus:
    """Simple fan-out pub/sub for VaultEvents within a single asyncio event loop."""

    def __init__(self) -> None:
        self._listeners: list[AsyncListener] = []
        self._lock = asyncio.Lock()

    async def subscribe(self, listener: AsyncListener) -> None:
        async with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    async def unsubscribe(self, listener: AsyncListener) -> None:
        async with self._lock:
            self._listeners = [l for l in self._listeners if l is not listener]

    async def publish(self, event: VaultEvent) -> None:
        async with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                await listener(event)
            except Exception:
                _rlog.exception("widget_notes", f"[EventBus] listener error for event type={event.type}")


__all__ = ["AsyncListener", "EventBus", "VaultEvent", "VaultEventType"]
