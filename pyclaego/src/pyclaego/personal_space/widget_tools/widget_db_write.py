"""widget_db_write — 向当前 widget 的 store 写入若干行。

LLM-facing schema:
    {
      "table": str,                        # required
      "rows": list<object>                 # required, non-empty
    }

非只读、不可并发（同表写入需要串行以保证顺序）。
"""

from __future__ import annotations

from typing import Any

from ...tool.base_tool import ToolResult, ToolStatus
from .base import WidgetTool


class WidgetDbWriteTool(WidgetTool):
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "Append rows to a table in this widget's local store. "
                "Returns the number of rows actually written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Target table name.",
                    },
                    "rows": {
                        "type": "array",
                        "description": "Array of row objects to append.",
                        "items": {"type": "object"},
                        "minItems": 1,
                    },
                },
                "required": ["table", "rows"],
            },
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            store = self._require_store()
        except RuntimeError as e:
            return ToolResult(status=ToolStatus.FAILED, error=str(e))

        table = kwargs.get("table")
        rows = kwargs.get("rows")
        if not isinstance(table, str) or not table:
            return ToolResult(
                status=ToolStatus.FAILED, error="`table` (string) is required"
            )
        if not isinstance(rows, list) or not rows:
            return ToolResult(
                status=ToolStatus.FAILED, error="`rows` must be a non-empty array"
            )
        if not all(isinstance(r, dict) for r in rows):
            return ToolResult(
                status=ToolStatus.FAILED, error="every row must be an object/dict"
            )

        try:
            written = await store.write(table=table, rows=rows)  # type: ignore[arg-type]
        except Exception as e:
            return ToolResult(status=ToolStatus.FAILED, error=f"write failed: {e}")

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"written": written},
            metadata={"table": table, "ps_id": self._ps_id, "widget_id": self._widget_id},
        )
