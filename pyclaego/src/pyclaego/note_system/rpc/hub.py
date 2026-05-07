"""NoteRpcHub — per-WebSocket-connection JSON-RPC 2.0 handler.

Lifecycle:
  hub = NoteRpcHub(websocket, default_doc_root)
  await hub.run()   # blocks until the WS closes, then cleans up

Protocol:
  Client → Server:  JsonRpcRequest   (method, id, params)
  Server → Client:  JsonRpcResponse  (id, result)
                    JsonRpcErrorResponse (id, error)
                    JsonRpcNotification  (method="vault.event", no id)

Vault lifecycle:
  vault.open   → acquire vault from NoteSystemManager, subscribe to EventBus
  vault.close  → unsubscribe, release vault
  (disconnect) → cleanup() automatically closes all open vaults
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...logging import get_running_log
from ..events import VaultEvent
from ..manager import NoteSystemManager
from ..vault import NoteVault
from .protocol import (
    APP_VAULT_NOT_OPEN,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    RpcException,
)
from .service import NoteRpcService

_rlog = get_running_log()


class NoteRpcHub:
    """Manages one WebSocket connection: parses JSON-RPC requests, dispatches to
    NoteRpcService, forwards VaultEvents as notifications, and cleans up vaults on
    disconnect."""

    def __init__(self, websocket: Any, default_doc_root: str) -> None:
        self._ws = websocket
        self._default_doc_root = default_doc_root
        self._vaults: dict[str, NoteVault] = {}          # vault_id → NoteVault
        self._listeners: dict[str, Any] = {}              # vault_id → event listener fn
        self._service = NoteRpcService()
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ── Public lifecycle ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Main message loop. Returns when the WebSocket disconnects."""
        from fastapi import WebSocketDisconnect  # local import to avoid circular deps
        try:
            while True:
                text = await self._ws.receive_text()
                task = asyncio.create_task(self._handle_message(text))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except WebSocketDisconnect:
            pass
        except Exception:
            _rlog.exception("widget_notes", "[NoteRpcHub] unexpected error in run()")
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Cancel in-flight tasks, unsubscribe from event buses, release all vaults."""
        # Cancel all pending dispatch tasks
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Release all open vaults
        nsm = NoteSystemManager.instance()
        for vault_id, vault in list(self._vaults.items()):
            listener = self._listeners.pop(vault_id, None)
            if listener is not None:
                try:
                    await vault.bus.unsubscribe(listener)
                except Exception:
                    pass
            try:
                await nsm.release(vault.root)
            except Exception:
                pass
        self._vaults.clear()

    # ── Message handling ─────────────────────────────────────────────────

    async def _handle_message(self, text: str) -> None:
        # Parse JSON
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            await self._send_error(None, PARSE_ERROR, "Parse error")
            return

        # Validate Request shape
        try:
            req = JsonRpcRequest.model_validate(data)
        except (ValidationError, Exception):
            await self._send_error(None, INVALID_REQUEST, "Invalid Request")
            return

        # Dispatch
        try:
            result = await self._dispatch(req.method, req.params)
            if req.id is not None:
                await self._send(JsonRpcResponse(id=req.id, result=result))
        except RpcException as exc:
            if req.id is not None:
                await self._send_error(req.id, exc.code, exc.message, exc.data)
        except Exception as exc:
            _rlog.exception("widget_notes", f"[NoteRpcHub] unhandled error for method={req.method!r}")
            if req.id is not None:
                await self._send_error(req.id, INTERNAL_ERROR, f"Internal error: {exc}")

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "vault.open":
            return await self._vault_open(params)
        if method == "vault.close":
            return await self._vault_close(params)

        # All other methods require an open vault
        vault_id: str | None = params.get("vault_id")
        if not vault_id or vault_id not in self._vaults:
            raise RpcException(
                APP_VAULT_NOT_OPEN,
                "vault_id not open; call vault.open first",
            )
        vault = self._vaults[vault_id]
        return await self._service.dispatch(method, params, vault)

    # ── Vault lifecycle ──────────────────────────────────────────────────

    async def _vault_open(self, params: dict[str, Any]) -> dict[str, Any]:
        doc_root: str = params.get("doc_root") or self._default_doc_root
        # Normalize to canonical path → use as vault_id
        vault_id = str(Path(doc_root).expanduser().resolve())

        if vault_id not in self._vaults:
            vault = await NoteSystemManager.instance().acquire(doc_root)
            self._vaults[vault_id] = vault

            # Subscribe to the vault's EventBus
            listener = self._make_event_listener(vault_id)
            self._listeners[vault_id] = listener
            await vault.bus.subscribe(listener)

        return {"vault_id": vault_id}

    async def _vault_close(self, params: dict[str, Any]) -> dict[str, Any]:
        vault_id: str | None = params.get("vault_id")
        if vault_id and vault_id in self._vaults:
            vault = self._vaults.pop(vault_id)
            listener = self._listeners.pop(vault_id, None)
            if listener is not None:
                try:
                    await vault.bus.unsubscribe(listener)
                except Exception:
                    pass
            try:
                await NoteSystemManager.instance().release(vault.root)
            except Exception:
                pass
        return {"ok": True}

    # ── Event forwarding ─────────────────────────────────────────────────

    def _make_event_listener(self, vault_id: str):
        async def _listener(event: VaultEvent) -> None:
            notif = JsonRpcNotification(
                method="vault.event",
                params={
                    "vault_id": vault_id,
                    "type": event.type,
                    "rel_path": event.rel_path,
                    "doc_id": event.doc_id,
                    "title": event.title,
                    "modified_at": event.modified_at,
                    "old_path": event.old_path,
                },
            )
            await self._send(notif)

        return _listener

    # ── Transport ────────────────────────────────────────────────────────

    async def _send(self, msg: JsonRpcResponse | JsonRpcErrorResponse | JsonRpcNotification) -> None:
        async with self._send_lock:
            try:
                await self._ws.send_text(msg.model_dump_json())
            except Exception:
                pass  # WebSocket may already be closing; errors are expected here

    async def _send_error(
        self,
        req_id: int | str | None,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        resp = JsonRpcErrorResponse(
            id=req_id,
            error=JsonRpcError(code=code, message=message, data=data),
        )
        await self._send(resp)


__all__ = ["NoteRpcHub"]
