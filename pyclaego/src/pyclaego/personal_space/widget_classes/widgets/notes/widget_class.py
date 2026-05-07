"""Notes WidgetHook — lifecycle integration for the NoteVault.

This file is auto-discovered by WidgetClassRegistry._load_hook_class():
  - It must define a class named `WidgetHook` (overriding the base class).
  - It must live at widget_classes/notes/widget_class.py.

Responsibilities:
  - on_create(): resolve doc_root, instantiate and start NoteVault
  - on_destroy(): shutdown NoteVault (closes DB + cancels watcher)
  - compute_highlight(): return doc/tag counts for Dashboard card badge
  - register_routes(): add notes REST + WS routes to the global /api/v2 router
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .....logging import get_running_log
from .....note_system import NoteSystemManager
from ...hook import WidgetHook as _BaseHook

_rlog = get_running_log()

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .....note_system import NoteVault


class WidgetHook(_BaseHook):
    def __init__(self, widget: Any) -> None:
        super().__init__(widget)
        self.vault: NoteVault | None = None
        self._doc_root: str | None = None  # canonical path used for release()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_create(self) -> None:
        cfg = self.widget.resolved_config or {}
        raw_root = cfg.get("doc_root") or ""
        if not raw_root or raw_root == "<widget_root>/notes":
            raw_root = str(self.widget.workspace_dir / "notes")
            _rlog.info(
                "widget_notes",
                f"[notes/{self.widget.widget_id}] doc_root defaulting to {raw_root}"
            )
        self.vault = await NoteSystemManager.instance().acquire(raw_root)
        # Store the canonical form for symmetric release
        self._doc_root = raw_root
        _rlog.info(
            "widget_notes",
            f"[notes/{self.widget.widget_id}] NoteVault acquired at {self.vault.root}"
        )

    async def on_destroy(self) -> None:
        if self._doc_root is not None:
            await NoteSystemManager.instance().release(self._doc_root)
            self._doc_root = None
            self.vault = None

    # ------------------------------------------------------------------
    # Dashboard highlight
    # ------------------------------------------------------------------

    def compute_highlight(self) -> dict[str, Any]:
        if self.vault is None:
            return {"status": "no_vault"}
        try:
            doc_count = sum(1 for _ in self.vault.root.rglob("*.md"))
        except Exception:
            doc_count = -1
        return {"doc_count": doc_count, "status": "ok"}

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    @classmethod
    def register_routes(cls, router: APIRouter) -> None:
        from fastapi import APIRouter as _APIRouter

        from . import routes as mod
        # REST routes wrapped under /api/v2
        rest_sub = _APIRouter(prefix="/api/v2", tags=["notes"])
        rest_sub.include_router(mod.notes_rest_router)
        router.include_router(rest_sub)
        # WS route registered directly (path already contains /ws/v2/...)
        router.include_router(mod.notes_ws_router)
        _rlog.info(
            "widget_notes",
            "[notes] custom routes registered"
        )
