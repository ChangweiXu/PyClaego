"""widget_emit — 让 widget 主动向外推送一个事件。

LLM-facing schema:
    {
      "channel": str,                       # required
      "payload": object                     # optional
    }

事件投递目标：
- 若 widget 在 ``__init__`` 阶段被注入了 ``emit_fn``（由 Widget 在 load 时
  传入），则直接 ``await emit_fn({...})``；
- 否则只在本地 store 留痕（写入 ``_emits`` 表）。

返回 ``{delivered, persisted}``，方便 LLM/调用方了解事件是否被实际广播。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ...tool.base_tool import ToolResult, ToolStatus
from .base import WidgetTool


class WidgetEmitTool(WidgetTool):
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = True

    EMIT_TABLE = "_emits"

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "Emit a structured event from this widget. "
                "Subscribers (UI viewers / dashboards) may receive it live; "
                "if the store supports it, the event is also persisted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Event channel name, e.g. 'progress', 'highlight'.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Arbitrary JSON-serialisable payload.",
                        "additionalProperties": True,
                    },
                },
                "required": ["channel"],
            },
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        channel = kwargs.get("channel")
        if not isinstance(channel, str) or not channel:
            return ToolResult(
                status=ToolStatus.FAILED, error="`channel` (string) is required"
            )
        payload = kwargs.get("payload") or {}
        if not isinstance(payload, dict):
            return ToolResult(
                status=ToolStatus.FAILED, error="`payload` must be an object/dict"
            )

        event = {
            "type": "widget_event",
            "ps_id": self._ps_id,
            "widget_id": self._widget_id,
            "channel": channel,
            "payload": payload,
            "ts": int(time.time() * 1000),
        }

        delivered = False
        if self._emit_fn is not None:
            try:
                res = self._emit_fn(event)
                if asyncio.iscoroutine(res):
                    await res
                delivered = True
            except Exception as e:
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error=f"emit_fn failed: {e}",
                    metadata={"event": event},
                )

        persisted = False
        if self._store is not None:
            try:
                await self._store.write(self.EMIT_TABLE, [dict(event)])
                persisted = True
            except Exception:
                # 写入失败不应影响投递结果，仅记 metadata
                persisted = False

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"delivered": delivered, "persisted": persisted},
            metadata={"channel": channel, "ps_id": self._ps_id, "widget_id": self._widget_id},
        )
