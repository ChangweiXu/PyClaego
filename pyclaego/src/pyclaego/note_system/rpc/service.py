"""NoteRpcService — method registry and dispatch for the note system RPC bus.

Each handler is an async function with signature:
    async (params: dict, vault: NoteVault) -> Any

Vault lifecycle methods (vault.open / vault.close) live in NoteRpcHub, not here.
The service only handles notes.* and system.* methods.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from ..vault import NoteVault
from .protocol import (
    APP_ALREADY_EXISTS,
    APP_NOT_FOUND,
    APP_PERMISSION_DENIED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RpcException,
)

Handler = Callable[[dict[str, Any], NoteVault], Coroutine[Any, Any, Any]]


def _require(params: dict, *keys: str) -> None:
    """Raise INVALID_PARAMS if any required key is missing."""
    missing = [k for k in keys if k not in params]
    if missing:
        raise RpcException(INVALID_PARAMS, f"Missing required params: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

async def _ping(params: dict, vault: NoteVault) -> dict:
    return {"pong": True}


async def _tree(params: dict, vault: NoteVault) -> dict:
    return {"tree": vault.file_tree()}


async def _read(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path")
    rel_path: str = params["rel_path"]
    json_content = await vault.read_json(rel_path)
    if json_content is None:
        raise RpcException(APP_NOT_FOUND, f"{rel_path!r} not found")
    meta = await vault.get_doc_meta(rel_path)
    return {"rel_path": rel_path, "content": json_content, "meta": meta}


async def _write(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path", "content")
    rel_path: str = params["rel_path"]
    content: dict = params["content"]
    if not isinstance(content, dict):
        raise RpcException(INVALID_PARAMS, "'content' must be a JSON object (Tiptap doc)")
    meta = await vault.write_json(rel_path, content)
    return meta.to_dict()


async def _create(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path")
    rel_path: str = params["rel_path"]
    content = params.get("content")
    if content is not None and isinstance(content, dict):
        # Tiptap JSON content provided
        meta = await vault.write_json(rel_path, content)
    elif content is not None and isinstance(content, str):
        # Legacy: XML string content
        meta = await vault.write(rel_path, content)
    else:
        # Empty doc
        meta = await vault.write(rel_path, "")
    return meta.to_dict()


async def _delete(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path")
    await vault.delete(params["rel_path"])
    return {"ok": True}


async def _rename(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path", "new_path")
    meta = await vault.rename(params["rel_path"], params["new_path"])
    return meta.to_dict()


async def _meta(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path")
    result = await vault.get_doc_meta(params["rel_path"])
    if result is None:
        raise RpcException(APP_NOT_FOUND, f"{params['rel_path']!r} not found")
    return result


async def _search(params: dict, vault: NoteVault) -> dict:
    _require(params, "query")
    q: str = params["query"]
    limit: int = int(params.get("limit", 20))
    limit = max(1, min(100, limit))
    results = await vault.search(q, limit=limit)
    return {"query": q, "results": results}


async def _graph(params: dict, vault: NoteVault) -> dict:
    return await vault.graph()


async def _backlinks(params: dict, vault: NoteVault) -> dict:
    _require(params, "rel_path")
    rel_path: str = params["rel_path"]
    links = await vault.backlinks(rel_path)
    return {"rel_path": rel_path, "backlinks": links}


async def _tags(params: dict, vault: NoteVault) -> dict:
    return {"tags": await vault.all_tags()}


async def _docs_by_tag(params: dict, vault: NoteVault) -> dict:
    _require(params, "tag_id")
    tag_id: int = int(params["tag_id"])
    docs = await vault.docs_by_tag(tag_id)
    return {"tag_id": tag_id, "docs": docs}


async def _resolve_link(params: dict, vault: NoteVault) -> dict:
    _require(params, "from_path", "target")
    return await vault.resolve_link(
        params["from_path"],
        params["target"],
        params.get("anchor", ""),
    )


async def _resolve_doc_id(params: dict, vault: NoteVault) -> dict:
    _require(params, "doc_id")
    result = await vault.resolve_doc_id(params["doc_id"])
    if result is None:
        raise RpcException(APP_NOT_FOUND, f"doc_id {params['doc_id']!r} not found")
    return result


async def _autocomplete(params: dict, vault: NoteVault) -> dict:
    _require(params, "prefix", "kind")
    return await vault.autocomplete(params["prefix"], params["kind"])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Handler] = {
    "system.ping": _ping,
    "notes.tree": _tree,
    "notes.read": _read,
    "notes.write": _write,
    "notes.create": _create,
    "notes.delete": _delete,
    "notes.rename": _rename,
    "notes.meta": _meta,
    "notes.search": _search,
    "notes.graph": _graph,
    "notes.backlinks": _backlinks,
    "notes.tags": _tags,
    "notes.docs_by_tag": _docs_by_tag,
    "notes.resolve_link": _resolve_link,
    "notes.resolve_doc_id": _resolve_doc_id,
    "notes.autocomplete": _autocomplete,
}


# ---------------------------------------------------------------------------
# NoteRpcService
# ---------------------------------------------------------------------------

class NoteRpcService:
    """Stateless dispatcher — maps method name → handler, then calls it."""

    async def dispatch(self, method: str, params: dict[str, Any], vault: NoteVault) -> Any:
        handler = _REGISTRY.get(method)
        if handler is None:
            raise RpcException(METHOD_NOT_FOUND, f"Method not found: {method!r}")
        try:
            return await handler(params, vault)
        except RpcException:
            raise
        except FileNotFoundError as exc:
            raise RpcException(APP_NOT_FOUND, str(exc)) from exc
        except FileExistsError as exc:
            raise RpcException(APP_ALREADY_EXISTS, str(exc)) from exc
        except PermissionError as exc:
            raise RpcException(APP_PERMISSION_DENIED, str(exc)) from exc
        except ValueError as exc:
            raise RpcException(INVALID_PARAMS, str(exc)) from exc
        except Exception as exc:
            raise RpcException(INTERNAL_ERROR, f"Internal error: {exc}") from exc


__all__ = ["NoteRpcService"]
