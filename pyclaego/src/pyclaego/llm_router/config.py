"""Configuration loader and validator for the LLM Router.

Reads the `llm_router` top-level key from pyclaego's `ConfigManager`
and produces typed, validated dataclasses. Duplicate aliases inside the
same protocol raise `RouterConfigError` at load time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyclaego.config import get_config

VALID_PROTOCOLS = ("openai", "anthropic", "gemini", "ollama")


class RouterConfigError(ValueError):
    """Raised when the llm_router config is missing/invalid."""


@dataclass(frozen=True)
class ModelEntry:
    alias: str           # what the client sends in `model`
    upstream_model: str  # what we forward as `model`


@dataclass(frozen=True)
class UpstreamConfig:
    id: str
    protocol: str        # one of VALID_PROTOCOLS
    base_url: str
    api_key: str
    headers: dict[str, str]
    models: tuple[ModelEntry, ...]


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 18790


@dataclass(frozen=True)
class StorageConfig:
    call_dump_dir: Path
    stats_db_path: Path
    dump_enabled: bool
    mask_keys: tuple[str, ...]


@dataclass(frozen=True)
class RouterConfig:
    server: ServerConfig
    storage: StorageConfig
    upstreams: tuple[UpstreamConfig, ...]


def _as_str(v: Any, *, name: str) -> str:
    if not isinstance(v, str):
        raise RouterConfigError(f"{name} must be a string, got {type(v).__name__}")
    return v


def _parse_models(raw: Any, *, upstream_id: str) -> tuple[ModelEntry, ...]:
    if not isinstance(raw, list) or not raw:
        raise RouterConfigError(
            f"upstream '{upstream_id}': models must be a non-empty list"
        )
    out: list[ModelEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RouterConfigError(
                f"upstream '{upstream_id}': models[{i}] must be a mapping"
            )
        alias = _as_str(item.get("alias"), name=f"upstream '{upstream_id}'.models[{i}].alias")
        upstream_model = _as_str(
            item.get("upstream_model"),
            name=f"upstream '{upstream_id}'.models[{i}].upstream_model",
        )
        if alias in seen:
            raise RouterConfigError(
                f"upstream '{upstream_id}': duplicate alias '{alias}' within same upstream"
            )
        seen.add(alias)
        out.append(ModelEntry(alias=alias, upstream_model=upstream_model))
    return tuple(out)


def _parse_upstream(raw: Any, *, idx: int) -> UpstreamConfig:
    if not isinstance(raw, dict):
        raise RouterConfigError(f"upstreams[{idx}] must be a mapping")
    uid = _as_str(raw.get("id"), name=f"upstreams[{idx}].id")
    protocol = _as_str(raw.get("protocol"), name=f"upstream '{uid}'.protocol")
    if protocol not in VALID_PROTOCOLS:
        raise RouterConfigError(
            f"upstream '{uid}': protocol must be one of {VALID_PROTOCOLS}, got '{protocol}'"
        )
    base_url = _as_str(raw.get("base_url"), name=f"upstream '{uid}'.base_url").rstrip("/")
    api_key = raw.get("api_key", "")
    if api_key is None:
        api_key = ""
    if not isinstance(api_key, str):
        raise RouterConfigError(f"upstream '{uid}': api_key must be a string")
    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise RouterConfigError(f"upstream '{uid}': headers must be a mapping")
    headers: dict[str, str] = {}
    for k, v in headers_raw.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float)):
            raise RouterConfigError(
                f"upstream '{uid}': header '{k}' must map string→scalar"
            )
        headers[k] = str(v)
    models = _parse_models(raw.get("models"), upstream_id=uid)
    return UpstreamConfig(
        id=uid,
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
        headers=headers,
        models=models,
    )


def _validate_no_cross_upstream_duplicate_alias(
    upstreams: tuple[UpstreamConfig, ...],
) -> None:
    """Aliases must be unique within the same protocol across all upstreams."""
    seen: dict[tuple[str, str], str] = {}  # (protocol, alias) -> upstream_id
    for up in upstreams:
        for m in up.models:
            key = (up.protocol, m.alias)
            if key in seen:
                raise RouterConfigError(
                    f"duplicate alias '{m.alias}' under protocol '{up.protocol}': "
                    f"upstreams '{seen[key]}' and '{up.id}'"
                )
            seen[key] = up.id


def _validate_unique_upstream_ids(upstreams: tuple[UpstreamConfig, ...]) -> None:
    seen: set[str] = set()
    for up in upstreams:
        if up.id in seen:
            raise RouterConfigError(f"duplicate upstream id '{up.id}'")
        seen.add(up.id)


def parse_router_config(raw: dict[str, Any]) -> RouterConfig:
    """Parse a raw `llm_router` dict (already env-resolved) into RouterConfig."""
    if not isinstance(raw, dict):
        raise RouterConfigError("llm_router config must be a mapping")

    server_raw = raw.get("server") or {}
    if not isinstance(server_raw, dict):
        raise RouterConfigError("llm_router.server must be a mapping")
    server = ServerConfig(
        host=str(server_raw.get("host", "127.0.0.1")),
        port=int(server_raw.get("port", 18790)),
    )

    storage_raw = raw.get("storage") or {}
    if not isinstance(storage_raw, dict):
        raise RouterConfigError("llm_router.storage must be a mapping")
    call_dump_dir = storage_raw.get("call_dump_dir")
    stats_db_path = storage_raw.get("stats_db_path")
    if not call_dump_dir or not stats_db_path:
        raise RouterConfigError(
            "llm_router.storage.call_dump_dir and stats_db_path are required"
        )
    mask_keys_raw = storage_raw.get("mask_keys") or []
    if not isinstance(mask_keys_raw, list):
        raise RouterConfigError("llm_router.storage.mask_keys must be a list")
    storage = StorageConfig(
        call_dump_dir=Path(str(call_dump_dir)).expanduser(),
        stats_db_path=Path(str(stats_db_path)).expanduser(),
        dump_enabled=bool(storage_raw.get("dump_enabled", True)),
        mask_keys=tuple(str(k).lower() for k in mask_keys_raw),
    )

    upstreams_raw = raw.get("upstreams") or []
    if not isinstance(upstreams_raw, list) or not upstreams_raw:
        raise RouterConfigError("llm_router.upstreams must be a non-empty list")
    upstreams = tuple(_parse_upstream(u, idx=i) for i, u in enumerate(upstreams_raw))
    _validate_unique_upstream_ids(upstreams)
    _validate_no_cross_upstream_duplicate_alias(upstreams)

    return RouterConfig(server=server, storage=storage, upstreams=upstreams)


def load_router_config(config: Any | None = None) -> RouterConfig:
    """Load the `llm_router` section from pyclaego's global ConfigManager.

    `config` may be a ConfigManager-like object (with `.get()`); when None,
    `pyclaego.config.get_config()` is used.
    """
    if config is None:
        config = get_config()
    raw = config.get("llm_router")
    if raw is None:
        raise RouterConfigError(
            "missing 'llm_router' section in pyclaego config; "
            "see .config.d/llm_router.yaml"
        )
    return parse_router_config(raw)
