"""logs_api.py — REST + WebSocket endpoints for the /dashboard/logs page.

REST
----
GET  /api/logs/tree         → { tree: LogTreeNode[] }
GET  /api/logs/file?path=…  → { path, content, size }

WebSocket
---------
/ws/logs
  Server pushes:
    { type: "tree_update", tree: LogTreeNode[] }   — on mount and on any FS change
    { type: "ping" }                                — keepalive every 30 s
  Client messages are ignored.
"""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ..logging import get_running_log
from ..security_executor.record_store import RecordStore

router = APIRouter(tags=["logs"])
_rlog = get_running_log()

_PING_INTERVAL = 30  # seconds between WS keepalive pings


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

def _store() -> RecordStore:
    return RecordStore.get_instance()


# ---------------------------------------------------------------------------
# REST: file tree
# ---------------------------------------------------------------------------

@router.get("/api/logs/tree")
async def get_logs_tree():
    """Return the full recursive file tree under log_root."""
    store = _store()
    tree = store.list_tree()
    return {"tree": tree}


# ---------------------------------------------------------------------------
# REST: file content
# ---------------------------------------------------------------------------

@router.get("/api/logs/file")
async def get_log_file(path: str = Query(..., description="Relative path inside log_root")):
    """Return the text content of a single log file."""
    store = _store()
    try:
        content = store.read_log_file(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except IsADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        # File too large
        raise HTTPException(status_code=413, detail=str(e))

    # Compute size from resolved path (safe — read_log_file already validated)
    log_root = store.get_log_root().resolve()
    target = (log_root / path).resolve()
    size = target.stat().st_size if target.exists() else len(content.encode())

    return {"path": path, "content": content, "size": size}


# ---------------------------------------------------------------------------
# WebSocket: live file tree watcher
# ---------------------------------------------------------------------------

async def _watch_and_push(websocket: WebSocket, log_root: Path) -> None:
    """Watch *log_root* with watchfiles and push tree_update messages."""
    try:
        from watchfiles import awatch  # type: ignore[import]
    except ImportError:
        # watchfiles not installed — fall back to polling every 10 s
        _rlog.warning("web_api", "[logs_ws] watchfiles not installed; using 10 s polling fallback")
        while True:
            await asyncio.sleep(10)
            store = _store()
            msg = json.dumps({"type": "tree_update", "tree": store.list_tree()})
            await websocket.send_text(msg)
        return

    async for _changes in awatch(str(log_root)):
        store = _store()
        try:
            msg = json.dumps({"type": "tree_update", "tree": store.list_tree()})
            await websocket.send_text(msg)
        except Exception:
            break


async def _ping_loop(websocket: WebSocket) -> None:
    """Send periodic pings to keep the WebSocket alive."""
    while True:
        await asyncio.sleep(_PING_INTERVAL)
        try:
            await websocket.send_text(json.dumps({"type": "ping"}))
        except Exception:
            break


@router.websocket("/ws/logs")
async def logs_websocket(websocket: WebSocket):
    """WebSocket endpoint — pushes tree_update events whenever log_root changes."""
    await websocket.accept()

    store = _store()
    log_root = store.get_log_root()

    # Ensure the log root exists so watchfiles doesn't fail immediately
    log_root.mkdir(parents=True, exist_ok=True)

    # Send the initial tree immediately after connection
    try:
        initial = json.dumps({"type": "tree_update", "tree": store.list_tree()})
        await websocket.send_text(initial)
    except Exception as exc:
        _rlog.warning("web_api", f"[logs_ws] Failed to send initial tree: {exc}")
        return

    watch_task = asyncio.create_task(_watch_and_push(websocket, log_root))
    ping_task = asyncio.create_task(_ping_loop(websocket))

    try:
        # Drain incoming messages (we don't act on them, but we must read to
        # detect client disconnection).
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _rlog.debug("web_api", f"[logs_ws] WebSocket closed: {exc}")
    finally:
        watch_task.cancel()
        ping_task.cancel()
        try:
            await asyncio.gather(watch_task, ping_task, return_exceptions=True)
        except Exception:
            pass
