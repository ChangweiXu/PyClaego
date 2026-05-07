"""NoteSystemManager — process-level ref-counted vault registry.

All notes widget instances share this singleton so that:
  - Two widgets pointing at the same doc_root reuse the same NoteVault instance
    (one SQLite connection, one watcher, one EventBus).
  - No write races between widgets sharing a public note directory.

Usage:
  vault = await NoteSystemManager.instance().acquire(doc_root)
  # ... use vault ...
  await NoteSystemManager.instance().release(doc_root)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging import get_running_log
from .vault import NoteVault

_rlog = get_running_log()


@dataclass
class _Entry:
    vault: NoteVault
    refcount: int


class NoteSystemManager:
    """Process-level singleton vault registry with ref-counting.

    Thread safety: designed for a single asyncio event loop.
    All public async methods are coroutines and safe to await concurrently.
    """

    _instance: NoteSystemManager | None = None

    def __init__(self) -> None:
        self._vaults: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> NoteSystemManager:
        """Return the process-level singleton, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def acquire(self, doc_root: str | Path) -> NoteVault:
        """Return the NoteVault for *doc_root*, starting it if necessary.

        Each call increments the ref-count; pair with a matching release().
        """
        canon = str(Path(doc_root).expanduser().resolve())

        async with self._lock:
            entry = self._vaults.get(canon)
            if entry is not None:
                entry.refcount += 1
                _rlog.info(
                    "note_system",
                    f"[NoteSystemManager] reuse vault refcount={entry.refcount} root={canon}",
                )
                return entry.vault
            # First acquire: create the vault (startup runs outside the lock below)
            vault = NoteVault(canon)

        # startup() is potentially slow (DB bootstrap + reconcile); run outside lock
        await vault.startup()

        async with self._lock:
            # Handle race: another coroutine may have completed startup for the same
            # root while we were outside the lock.
            existing = self._vaults.get(canon)
            if existing is not None:
                # Another coroutine won the race; discard our vault and use theirs.
                await vault.shutdown()
                existing.refcount += 1
                _rlog.info(
                    "note_system",
                    f"[NoteSystemManager] race resolved, reuse vault refcount={existing.refcount} root={canon}",
                )
                return existing.vault
            self._vaults[canon] = _Entry(vault=vault, refcount=1)
            _rlog.info("note_system", f"[NoteSystemManager] new vault registered root={canon}")
            return vault

    async def release(self, doc_root: str | Path) -> None:
        """Decrement ref-count; shut down the vault when it reaches zero."""
        canon = str(Path(doc_root).expanduser().resolve())
        vault_to_stop: NoteVault | None = None

        async with self._lock:
            entry = self._vaults.get(canon)
            if entry is None:
                return  # idempotent: no-op if already released
            entry.refcount -= 1
            _rlog.info(
                "note_system",
                f"[NoteSystemManager] release refcount={entry.refcount} root={canon}",
            )
            if entry.refcount <= 0:
                vault_to_stop = entry.vault
                del self._vaults[canon]

        if vault_to_stop is not None:
            await vault_to_stop.shutdown()
            _rlog.info("note_system", f"[NoteSystemManager] vault shutdown root={canon}")

    def get(self, doc_root: str | Path) -> NoteVault | None:
        """Synchronous lookup — returns the live vault or None (no refcount change)."""
        canon = str(Path(doc_root).expanduser().resolve())
        entry = self._vaults.get(canon)
        return entry.vault if entry else None

    def list_active(self) -> list[dict[str, Any]]:
        """Return a snapshot of all active vault entries for introspection."""
        return [
            {
                "root": canon,
                "refcount": entry.refcount,
                "started": entry.vault._started,
            }
            for canon, entry in self._vaults.items()
        ]

    async def shutdown_all(self) -> None:
        """Shutdown every live vault — call on process exit."""
        async with self._lock:
            items = list(self._vaults.items())
            self._vaults.clear()
        for canon, entry in items:
            try:
                await entry.vault.shutdown()
                _rlog.info("note_system", f"[NoteSystemManager] shutdown_all: stopped root={canon}")
            except Exception:
                _rlog.exception(
                    "note_system",
                    f"[NoteSystemManager] shutdown_all: error stopping root={canon}",
                )


__all__ = ["NoteSystemManager"]
