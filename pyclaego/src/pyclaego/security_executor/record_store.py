"""record_store.py — Centralised record-writing and TTL-based auto-deletion.

All JSON records produced by SecurityHandler are written through this module.
A background asyncio task sweeps the log tree at a fixed interval and deletes
any record file whose mtime is older than ``ttl_hours``.

Public API
----------
``RecordStore.write_llm_call(...)``
``RecordStore.write_tool_call(...)``
``RecordStore.write_subagent_llm_call(...)``
``RecordStore.write_subagent_tool_call(...)``

Module-level helpers (re-exported for callers that still need them):
``serialize_llm_response(response)``
``summarize_content_parts(content_parts)``
"""

import asyncio
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ..config import PYCLAEGO_DEFAULT_LOGS_ROOT, get_config
from ..llm import ToolDefinition, serialize_llm_response, summarize_content_parts
from ..logging import get_running_log

_rlog = get_running_log()

# ---------------------------------------------------------------------------
# Serialisation helpers — re-exported so callers that imported them from here
# previously don't need a two-step migration.
# ---------------------------------------------------------------------------
# (serialize_llm_response and summarize_content_parts now live in llm/types.py
#  and are imported above; they are part of this module's public API via the
#  import, so ``from .record_store import serialize_llm_response`` still works.)


# ---------------------------------------------------------------------------
# RecordStore
# ---------------------------------------------------------------------------

# How often the sweep loop wakes up (seconds). Independent of TTL granularity.
_SWEEP_INTERVAL_SECONDS = 3600  # 1 hour


class RecordStore:
    """Writes JSON call records and auto-deletes files older than ``ttl_hours``.

    Parameters
    ----------
    log_root:
        Root directory for all records (same value as ``SecurityHandler.log_root``).
    ttl_hours:
        How many hours to keep a record file.  ``0`` disables auto-deletion.
    """

    _instance: Optional["RecordStore"] = None

    @classmethod
    def get_instance(cls) -> "RecordStore":
        """Return the process-wide singleton, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        config = get_config()
        logging_config = config.get("logging", {})
        log_root = logging_config.get("log_root", PYCLAEGO_DEFAULT_LOGS_ROOT)
        record_store_config = logging_config.get("record_store", {})
        ttl_hours = record_store_config.get("record_ttl_hours", 0)

        self.log_root = Path(log_root).expanduser()
        self._ttl_hours = float(ttl_hours)
        self._sweep_task: asyncio.Task | None = None  # type: ignore[type-arg]

        _rlog.info(
            "core_service",
            f"[RecordStore] Initialized with log_root={self.log_root}, "
            f"ttl_hours={self._ttl_hours}"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background TTL sweep loop.

        Call once after the event loop is running (e.g. in
        ``SecurityHandler.__init__`` wrapped in ``asyncio.get_event_loop()``
        or from an ``async`` startup routine).
        Idempotent: calling again while already running is a no-op.
        """
        if self._ttl_hours <= 0:
            return
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        self._sweep_task = loop.create_task(self._sweep_loop())
        _rlog.info(
            "core_service",
            f"[RecordStore] TTL sweep started "
            f"(ttl={self._ttl_hours}h, interval={_SWEEP_INTERVAL_SECONDS}s)"
        )

    def stop(self) -> None:
        """Cancel the sweep loop. Call on intentional shutdown."""
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            self._sweep_task = None

    # ------------------------------------------------------------------
    # Public write methods
    # ------------------------------------------------------------------

    def write_llm_call(
        self,
        session_id: str,
        llm_id: str,
        messages: list[dict[str, Any]],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        response: Any = "",
        error: str = "",
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict | None:
        """Write an LLM call record to ``llm_calls/{session_id}/``."""
        try:
            record_dir = self.log_root / "llm_calls" / session_id
            record_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = record_dir / f"{ts}-{session_id}-{llm_id}.json"

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "llm_id": llm_id,
                "tool_list": [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in tool_list
                ] if tool_list else None,
                "messages": messages,
                "security_decision": security_decision,
                "success": success,
                "response": serialize_llm_response(response),
                "error": error,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tool_choice": tool_choice,
                "kwargs": self._safe_kwargs(kwargs),
            }
            self._write_json(filepath, record)
            _rlog.info("core_service", f"[RecordStore] LLM 调用记录已保存: {filepath}")
            return record

        except Exception as e:
            _rlog.error("core_service",
                        f"[RecordStore] 保存 LLM 调用记录失败: {e}\n{traceback.format_exc()}")
            return None

    def write_tool_call(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        output: str = "",
        error: str = "",
        content_parts: list | None = None,
    ) -> dict | None:
        """Write a tool call record to ``tool_calls/{session_id}/``."""
        try:
            record_dir = self.log_root / "tool_calls" / session_id
            record_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = record_dir / f"{ts}-{session_id}-{tool_name}.json"

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "security_decision": security_decision,
                "success": success,
                "output": output,
                "error": error,
                "content_parts_summary": summarize_content_parts(content_parts),
            }
            self._write_json(filepath, record)
            _rlog.info("core_service", f"[RecordStore] 工具调用记录已保存: {filepath}")
            return record

        except Exception as e:
            _rlog.error("core_service",
                        f"[RecordStore] 保存工具调用记录失败: {e}\n{traceback.format_exc()}")
            return None

    def write_subagent_llm_call(
        self,
        session_id: str,
        subagent_id: str,
        llm_id: str,
        messages: list[dict[str, Any]],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        response: Any = "",
        error: str = "",
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict | None:
        """Write a subagent LLM call record to
        ``llm_calls/{session_id}/subagents/{subagent_id}/``."""
        try:
            record_dir = (
                self.log_root / "llm_calls" / session_id / "subagents" / subagent_id
            )
            record_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = record_dir / f"{ts}-{subagent_id}-{llm_id}.json"

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "session_id": session_id,
                "subagent_id": subagent_id,
                "llm_id": llm_id,
                "tool_list": [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in tool_list
                ] if tool_list else None,
                "messages": messages,
                "security_decision": security_decision,
                "success": success,
                "response": serialize_llm_response(response),
                "error": error,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tool_choice": tool_choice,
                "kwargs": self._safe_kwargs(kwargs),
            }
            self._write_json(filepath, record)
            _rlog.info("core_service", f"[RecordStore] 子Agent LLM记录已保存: {filepath}")
            return record

        except Exception as e:
            _rlog.error("core_service",
                        f"[RecordStore] 保存子Agent LLM记录失败: {e}\n{traceback.format_exc()}")
            return None

    def write_subagent_tool_call(
        self,
        session_id: str,
        subagent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        output: str = "",
        error: str = "",
        content_parts: list | None = None,
    ) -> dict | None:
        """Write a subagent tool call record to
        ``tool_calls/{session_id}/subagents/{subagent_id}/``."""
        try:
            record_dir = (
                self.log_root / "tool_calls" / session_id / "subagents" / subagent_id
            )
            record_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = record_dir / f"{ts}-{subagent_id}-{tool_name}.json"

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "session_id": session_id,
                "subagent_id": subagent_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "security_decision": security_decision,
                "success": success,
                "output": output,
                "error": error,
                "content_parts_summary": summarize_content_parts(content_parts),
            }
            self._write_json(filepath, record)
            _rlog.info("core_service", f"[RecordStore] 子Agent工具记录已保存: {filepath}")
            return record

        except Exception as e:
            _rlog.error("core_service",
                        f"[RecordStore] 保存子Agent工具记录失败: {e}\n{traceback.format_exc()}")
            return None

    def write_memory_tool_call(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        start_timestamp: str,
        end_timestamp: str,
        success: bool,
        output: str = "",
        error: str = "",
    ) -> None:
        """Write a memory tool call record to ``memory_tool_calls/{session_id}/``."""
        try:
            record_dir = self.log_root / "memory_tool_calls" / session_id
            record_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = record_dir / f"{ts}-{session_id}-{tool_name}.json"

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "success": success,
                "output": output,
                "error": error,
            }
            self._write_json(filepath, record)
            _rlog.info("core_service", f"[RecordStore] 记忆工具调用记录已保存: {filepath}")

        except Exception as e:
            _rlog.error("core_service",
                        f"[RecordStore] 保存记忆工具调用记录失败: {e}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------
    # Log tree / file access (used by the dashboard /logs page)
    # ------------------------------------------------------------------

    def get_log_root(self) -> Path:
        """Return the absolute log_root path."""
        return self.log_root

    def list_tree(self) -> list:
        """Walk log_root and return a recursive list of tree-node dicts.

        Each node has:
            name (str), path (str, relative to log_root),
            type ('file' | 'directory'),
            size (int, files only), mtime (ISO-8601 str, files only),
            children (list, directories only)
        """
        def _walk(p: Path, rel: Path) -> dict:
            if p.is_file():
                stat = p.stat()
                return {
                    "name": p.name,
                    "path": str(rel).replace("\\", "/"),
                    "type": "file",
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            else:
                children = []
                try:
                    entries = sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name))
                    for child in entries:
                        if not child.name.startswith("."):
                            children.append(_walk(child, rel / child.name))
                except PermissionError:
                    pass
                return {
                    "name": p.name,
                    "path": str(rel).replace("\\", "/"),
                    "type": "directory",
                    "children": children,
                }

        if not self.log_root.exists():
            return []
        result = []
        try:
            entries = sorted(self.log_root.iterdir(), key=lambda c: (c.is_file(), c.name))
            for child in entries:
                if not child.name.startswith("."):
                    result.append(_walk(child, Path(child.name)))
        except PermissionError:
            pass
        return result

    _MAX_LOG_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

    def read_log_file(self, rel_path: str) -> str:
        """Return the content of a log file at *rel_path* (relative to log_root).

        Raises
        ------
        ValueError
            If *rel_path* attempts to escape log_root (path traversal).
        PermissionError
            If the file size exceeds _MAX_LOG_FILE_BYTES.
        FileNotFoundError
            If the path does not exist or is not a file.
        """
        target = (self.log_root / rel_path).resolve()
        log_root_resolved = self.log_root.resolve()
        # Security: ensure the resolved path stays within log_root
        try:
            target.relative_to(log_root_resolved)
        except ValueError:
            raise ValueError(f"Path '{rel_path}' escapes log_root")
        if not target.exists():
            raise FileNotFoundError(f"'{rel_path}' does not exist")
        if not target.is_file():
            raise IsADirectoryError(f"'{rel_path}' is a directory, not a file")
        size = target.stat().st_size
        if size > self._MAX_LOG_FILE_BYTES:
            raise PermissionError(
                f"File '{rel_path}' is {size} bytes, which exceeds the "
                f"{self._MAX_LOG_FILE_BYTES // (1024 * 1024)} MB read limit"
            )
        return target.read_text(encoding="utf-8", errors="replace")

    def write_subagent_memory_tool_call(
        self,
        session_id: str,
        subagent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        start_timestamp: str,
        end_timestamp: str,
        success: bool,
        output: str = "",
        error: str = "",
    ) -> None:
        """Write a subagent memory tool call record to
        ``memory_tool_calls/{session_id}/subagents/{subagent_id}/``."""
        try:
            record_dir = (
                self.log_root / "memory_tool_calls" / session_id / "subagents" / subagent_id
            )
            record_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = record_dir / f"{ts}-{subagent_id}-{tool_name}.json"

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "session_id": session_id,
                "subagent_id": subagent_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "success": success,
                "output": output,
                "error": error,
            }
            self._write_json(filepath, record)
            _rlog.info("core_service", f"[RecordStore] 子Agent记忆工具记录已保存: {filepath}")

        except Exception as e:
            _rlog.error("core_service",
                        f"[RecordStore] 保存子Agent记忆工具记录失败: {e}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------
    # TTL sweep
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Periodically delete record files older than ttl_hours."""
        while True:
            self._sweep_once()
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)

    def _sweep_once(self) -> None:
        """Delete all JSON records under log_root whose mtime exceeds ttl_hours,
        then recursively remove empty directories."""
        if self._ttl_hours <= 0:
            return

        cutoff = datetime.now() - timedelta(hours=self._ttl_hours)
        deleted = 0
        errors = 0

        # ── Phase 1: delete expired JSON files ──────────────────────────────
        for pattern in (
            "llm_calls/**/*.json",
            "tool_calls/**/*.json",
            "memory_tool_calls/**/*.json",
            "security_logs/**/*.jsonl",
            "running/**/*.log",
        ):
            for path in self.log_root.glob(pattern):
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime)
                    if mtime < cutoff:
                        path.unlink(missing_ok=True)
                        deleted += 1
                except Exception as e:
                    errors += 1
                    _rlog.error(
                        "core_service",
                        f"[RecordStore] sweep delete error on {path}: {e}\n{traceback.format_exc()}",
                    )

        # ── Phase 2: bottom-up removal of empty directories ─────────────────
        top_dirs = [
            self.log_root / d for d in (
                "llm_calls",
                "tool_calls",
                "memory_tool_calls",
                "security_logs",
                "running",
            )
        ]

        def _prune_empty(dir_path: Path) -> None:
            """Recursively prune empty subdirectories, then self if empty."""
            try:
                children = list(dir_path.iterdir())
            except OSError:
                return

            # Process subdirectories first (depth-first)
            for child in children:
                if child.is_dir():
                    _prune_empty(child)

            # After children are handled, try to remove self if empty
            try:
                if dir_path != self.log_root and not any(dir_path.iterdir()):
                    dir_path.rmdir()
            except OSError:
                pass

        for td in top_dirs:
            if td.exists() and td.is_dir():
                _prune_empty(td)

        if deleted or errors:
            _rlog.info(
                "core_service",
                f"[RecordStore] sweep complete: deleted={deleted}, errors={errors}",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_kwargs(kwargs: dict[str, Any] | None) -> dict[str, Any]:
        """Serialise kwargs values to JSON-safe types (non-serialisable → str)."""
        result: dict[str, Any] = {}
        for k, v in (kwargs or {}).items():
            try:
                json.dumps(v)
                result[k] = v
            except (TypeError, ValueError):
                result[k] = str(v)
        return result

    @staticmethod
    def _write_json(filepath: Path, record: dict[str, Any]) -> None:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
