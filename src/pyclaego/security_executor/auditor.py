"""SecurityAuditor — security event logging (split out of SecurityMonitor)."""

from __future__ import annotations

import asyncio
import json
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from ..logging import get_running_log

_rlog = get_running_log()


class SecurityAuditor:
    """Record security events: bounded in-memory ring buffer + async JSONL file sink."""

    def __init__(
        self,
        log_root: Path,
        log_enabled: bool = True,
        event_buffer_size: int = 1000,
    ):
        self.log_root = Path(log_root).expanduser()
        self.log_enabled = log_enabled
        self.event_buffer_size = event_buffer_size

        # In-memory ring buffer (bounded to avoid unbounded memory growth)
        self.events: Deque[Dict[str, Any]] = deque(maxlen=event_buffer_size)

        if self.log_enabled:
            try:
                (self.log_root / "security_logs").mkdir(parents=True, exist_ok=True)
            except Exception as e:
                _rlog.error(
                    "core_service",
                    f"[SecurityAuditor] Failed to create log directory: {e}",
                )
                self.log_enabled = False

    async def log(self, event: Dict[str, Any]) -> None:
        """Append an event to memory and asynchronously persist it to disk."""
        try:
            self.events.append(event)
            if not self.log_enabled:
                return
            await asyncio.to_thread(self._write_event, event)
        except Exception as e:
            _rlog.error(
                "core_service",
                f"[SecurityAuditor] log() failed: {e}\n{traceback.format_exc()}",
            )

    # ---------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------

    def _write_event(self, event: Dict[str, Any]) -> None:
        try:
            date_str = datetime.now().strftime("%Y%m%d")
            path = self.log_root / "security_logs" / f"security_{date_str}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            _rlog.error(
                "core_service",
                f"[SecurityAuditor] Failed to write event: {e}",
            )

    def snapshot(self, limit: Optional[int] = None) -> list:
        """Return a shallow copy of recent events (optionally trimmed)."""
        if limit is None or limit >= len(self.events):
            return list(self.events)
        return list(self.events)[-limit:]
