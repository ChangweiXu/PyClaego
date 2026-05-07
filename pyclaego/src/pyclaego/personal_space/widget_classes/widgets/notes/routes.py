"""FastAPI router for the notes widget class.

All routes are under:
  REST:  /api/v2/personal_spaces/{ps_id}/widgets/{widget_id}/notes/...
  WS:    /ws/v2/notes/{ps_id}/{widget_id}/events

Route handlers resolve the NoteVault via:
  ps.get_widget(widget_id).hook.vault
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .....logging import get_running_log
from .events import VaultEvent

_rlog = get_running_log()

notes_rest_router = APIRouter()  # REST endpoints (registered under /api/v2 prefix)
notes_ws_router = APIRouter()    # WebSocket endpoints (registered at root, no prefix)


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def _get_psm():
    """Get the PersonalSpaceManager singleton (web process copy)."""
    from .....config import get_config
    from ....manager import PersonalSpaceManager
    ps_section = get_config().get("personal_space") or {}
    root = ps_section.get("root_path")
    max_active = ps_section.get("max_active")
    kwargs = {}
    if root is not None:
        kwargs["root_path"] = Path(root).expanduser()
    if max_active is not None:
        kwargs["max_active"] = int(max_active)
    return PersonalSpaceManager.instance(**kwargs)


async def _get_vault(ps_id: str, widget_id: str):
    """Load widget and return its NoteVault, or raise HTTPException."""
    try:
        ps = await _get_psm().get(ps_id)
        widget = await ps.get_widget(widget_id)
    except KeyError:
        raise HTTPException(404, f"Widget {widget_id!r} not found in PS {ps_id!r}")
    except Exception as e:
        raise HTTPException(400, str(e))
    hook = widget.hook
    if hook is None or not hasattr(hook, "vault"):
        raise HTTPException(400, f"Widget {widget_id!r} has no NoteVault (not a notes widget?)")
    return hook.vault


# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------

class CreateFileBody(BaseModel):
    rel_path: str
    content: str = ""  # .bdx XML string; empty = create blank doc


class WriteFileBody(BaseModel):
    content: str  # .bdx XML string


class RenameBody(BaseModel):
    new_path: str


class LibrarianChatBody(BaseModel):
    message: str
    history: list[dict] = []  # [{role, content}, ...]


# ---------------------------------------------------------------------------
# REST — file tree
# ---------------------------------------------------------------------------

_PREFIX = "/personal_spaces/{ps_id}/widgets/{widget_id}/notes"


@notes_rest_router.get(_PREFIX + "/files", tags=["notes"])
async def list_files(ps_id: str, widget_id: str):
    vault = await _get_vault(ps_id, widget_id)
    return {"tree": vault.file_tree()}


@notes_rest_router.post(_PREFIX + "/files", tags=["notes"])
async def create_file(ps_id: str, widget_id: str, body: CreateFileBody):
    vault = await _get_vault(ps_id, widget_id)
    try:
        meta = await vault.write(body.rel_path, body.content)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return meta.to_dict()


@notes_rest_router.get(_PREFIX + "/files/{rel_path:path}/backlinks", tags=["notes"])
async def get_backlinks(ps_id: str, widget_id: str, rel_path: str):
    """Return all docs that link to rel_path, with block_anchor and snippet."""
    vault = await _get_vault(ps_id, widget_id)
    try:
        links = await vault.backlinks(rel_path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"rel_path": rel_path, "backlinks": links}


@notes_rest_router.get(_PREFIX + "/files/{rel_path:path}", tags=["notes"])
async def read_file(ps_id: str, widget_id: str, rel_path: str):
    vault = await _get_vault(ps_id, widget_id)
    try:
        body = await vault.read(rel_path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body is None:
        raise HTTPException(404, f"{rel_path!r} not found")
    meta = await vault.get_doc_meta(rel_path)
    # Return JSON envelope so meta and content are accessible together.
    # content is the raw .bdx XML string.
    return {"rel_path": rel_path, "content": body, "meta": meta}


@notes_rest_router.put(_PREFIX + "/files/{rel_path:path}", tags=["notes"])
async def write_file(ps_id: str, widget_id: str, rel_path: str, body: WriteFileBody):
    vault = await _get_vault(ps_id, widget_id)
    try:
        meta = await vault.write(rel_path, body.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return meta.to_dict()


@notes_rest_router.delete(_PREFIX + "/files/{rel_path:path}", tags=["notes"])
async def delete_file(ps_id: str, widget_id: str, rel_path: str):
    vault = await _get_vault(ps_id, widget_id)
    try:
        await vault.delete(rel_path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@notes_rest_router.patch(_PREFIX + "/files/{rel_path:path}/rename", tags=["notes"])
async def rename_file(ps_id: str, widget_id: str, rel_path: str, body: RenameBody):
    vault = await _get_vault(ps_id, widget_id)
    try:
        meta = await vault.rename(rel_path, body.new_path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (FileExistsError, ValueError) as e:
        raise HTTPException(409, str(e))
    return meta.to_dict()


# ---------------------------------------------------------------------------
# REST — search / graph / tags / backlinks / resolve_link
# ---------------------------------------------------------------------------

@notes_rest_router.get(_PREFIX + "/search", tags=["notes"])
async def search(
    ps_id: str,
    widget_id: str,
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    vault = await _get_vault(ps_id, widget_id)
    results = await vault.search(q, limit=limit)
    return {"query": q, "results": results}


@notes_rest_router.get(_PREFIX + "/graph", tags=["notes"])
async def get_graph(ps_id: str, widget_id: str):
    vault = await _get_vault(ps_id, widget_id)
    return await vault.graph()


@notes_rest_router.get(_PREFIX + "/tags", tags=["notes"])
async def list_tags(ps_id: str, widget_id: str):
    vault = await _get_vault(ps_id, widget_id)
    return {"tags": await vault.all_tags()}


@notes_rest_router.get(_PREFIX + "/tags/{tag_id}/docs", tags=["notes"])
async def docs_by_tag(ps_id: str, widget_id: str, tag_id: int):
    vault = await _get_vault(ps_id, widget_id)
    return {"tag_id": tag_id, "docs": await vault.docs_by_tag(tag_id)}


@notes_rest_router.get(_PREFIX + "/resolve_link", tags=["notes"])
async def resolve_link(
    ps_id: str,
    widget_id: str,
    from_path: str = Query(...),
    target: str = Query(...),
    anchor: str = Query(""),
):
    """Resolve a link target relative to from_path.  Returns {rel_path, block_id, exists}."""
    vault = await _get_vault(ps_id, widget_id)
    return await vault.resolve_link(from_path, target, anchor)


# ---------------------------------------------------------------------------
# REST — Librarian chatbot
# ---------------------------------------------------------------------------

@notes_rest_router.post(_PREFIX + "/librarian/chat", tags=["notes"])
async def librarian_chat(ps_id: str, widget_id: str, body: LibrarianChatBody):
    """Send a message to the Librarian and receive a reply.

    The engine runs an LLM tool-calling loop with vault_search / vault_read /
    vault_list / vault_create and returns the final assistant text.
    """
    vault = await _get_vault(ps_id, widget_id)
    from .librarian import LibrarianEngine
    engine = LibrarianEngine(vault)
    reply = await engine.chat(body.message, body.history)
    return {"reply": reply}


# ---------------------------------------------------------------------------
# WebSocket — JSON-RPC 2.0 service bus
# ---------------------------------------------------------------------------

@notes_ws_router.websocket("/ws/v2/notes/{ps_id}/{widget_id}/rpc")
async def notes_rpc_ws(ps_id: str, widget_id: str, websocket: WebSocket):
    """Single WebSocket carrying JSON-RPC 2.0 requests/responses and
    server-push VaultEvent notifications (method='vault.event')."""
    await websocket.accept()
    try:
        vault = await _get_vault(ps_id, widget_id)
    except HTTPException as e:
        await websocket.send_text(json.dumps({
            "jsonrpc": "2.0",
            "method": "error",
            "params": {"message": e.detail},
        }))
        await websocket.close()
        return

    from .....note_system.rpc import NoteRpcHub
    hub = NoteRpcHub(websocket, str(vault.root))
    await hub.run()


# ---------------------------------------------------------------------------
# WebSocket — vault event stream
# ---------------------------------------------------------------------------

@notes_ws_router.websocket("/ws/v2/notes/{ps_id}/{widget_id}/events")
async def notes_events_ws(ps_id: str, widget_id: str, websocket: WebSocket):
    """Push VaultEvents to the browser as JSON frames."""
    await websocket.accept()
    vault = None
    try:
        vault = await _get_vault(ps_id, widget_id)
    except HTTPException as e:
        await websocket.send_text(json.dumps({"type": "error", "detail": e.detail}))
        await websocket.close()
        return

    queue: asyncio.Queue[VaultEvent] = asyncio.Queue()

    async def _on_event(event: VaultEvent) -> None:
        await queue.put(event)

    await vault.bus.subscribe(_on_event)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                payload = {
                    "type": event.type,
                    "rel_path": event.rel_path,
                    "doc_id": event.doc_id,
                    "title": event.title,
                    "modified_at": event.modified_at,
                    "old_path": event.old_path,
                }
                await websocket.send_text(json.dumps(payload))
            except asyncio.TimeoutError:
                # heartbeat ping
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        _rlog.exception("widget_notes", "[notes_events_ws] error")
    finally:
        await vault.bus.unsubscribe(_on_event)
        try:
            await websocket.close()
        except Exception:
            pass
