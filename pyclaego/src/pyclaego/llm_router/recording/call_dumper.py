"""Per-call JSON dump writer.

One JSON file per call under {call_dump_dir}/YYYYMMDD/HHMMSS_<id>.json.
Writes happen on a thread to avoid blocking the asyncio loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from ..config import StorageConfig
from .masker import mask_body, mask_headers, mask_query_params

logger = logging.getLogger(__name__)
_rlog = get_running_log()


@dataclass
class CallRecord:
    # Routing
    protocol: str
    alias: str
    upstream_id: str
    upstream_model: str

    # Request as we forwarded it (already-masked just before dump)
    request: dict[str, Any] = field(default_factory=dict)
    # Response as upstream returned it
    response: dict[str, Any] = field(default_factory=dict)
    # Timing
    timing: dict[str, Any] = field(default_factory=dict)
    # Token usage (may be None values)
    usage: dict[str, Any] = field(default_factory=dict)
    # Whether this was a streaming call
    stream: bool = False
    # Error class name if forwarding failed
    error: str | None = None
    # Merged complete response body for stream calls (protocol-native unary shape)
    merged_body: dict[str, Any] | None = None


class CallDumper:
    def __init__(self, storage: StorageConfig) -> None:
        self._storage = storage
        self._enabled = storage.dump_enabled
        self._root = storage.call_dump_dir
        self._mask_keys = storage.mask_keys

    async def startup(self) -> None:
        if self._enabled:
            self._root.mkdir(parents=True, exist_ok=True)

    def _build_path(self, protocol: str, alias: str) -> Path:
        now = datetime.now(timezone.utc)
        date_dir = self._root / now.strftime("%Y%m%d")
        # Half-hour bucket subdirectory: HH00 or HH30
        minute_bucket = "00" if now.minute < 30 else "30"
        bucket_dir = date_dir / f"{now.strftime('%H')}{minute_bucket}"
        bucket_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%H%M%S")
        rand = secrets.token_hex(3)
        # Sanitize alias for filename
        safe_alias = "".join(
            c if c.isalnum() or c in "-._" else "_" for c in alias
        ) or "unknown"
        return bucket_dir / f"{ts}_{rand}_{protocol}_{safe_alias}.json"

    def _redact_record(self, record: CallRecord) -> dict[str, Any]:
        rec = asdict(record)
        req = rec.get("request") or {}
        if "headers" in req and isinstance(req["headers"], dict):
            req["headers"] = mask_headers(req["headers"], self._mask_keys)
        if "params" in req and isinstance(req["params"], dict):
            req["params"] = mask_query_params(req["params"], self._mask_keys)
        if "body" in req:
            req["body"] = mask_body(req["body"], self._mask_keys)
        rec["request"] = req

        resp = rec.get("response") or {}
        if "headers" in resp and isinstance(resp["headers"], dict):
            resp["headers"] = mask_headers(resp["headers"], self._mask_keys)
        if "body" in resp:
            resp["body"] = mask_body(resp["body"], self._mask_keys)
        rec["response"] = resp
        if rec.get("merged_body"):
            rec["merged_body"] = mask_body(rec["merged_body"], self._mask_keys)
        return rec

    async def write(self, record: CallRecord) -> Path | None:
        if not self._enabled:
            return None
        path = self._build_path(record.protocol, record.alias)
        payload = self._redact_record(record)
        merged_body = record.merged_body

        def _write() -> None:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, path)
            # Write merged stream body alongside if present.
            if merged_body:
                merged_payload = mask_body(deepcopy(merged_body), self._mask_keys)
                merged_path = path.with_name(path.stem + ".merged.json")
                tmp2 = merged_path.with_suffix(merged_path.suffix + ".tmp")
                with open(tmp2, "w", encoding="utf-8") as f:
                    json.dump(merged_payload, f, ensure_ascii=False, indent=2, default=str)
                os.replace(tmp2, merged_path)

        try:
            await asyncio.to_thread(_write)
            _rlog.debug("llm_router", f"[dumper] saved to {path}")
            return path
        except Exception:
            logger.exception("failed to write call dump %s", path)
            return None
