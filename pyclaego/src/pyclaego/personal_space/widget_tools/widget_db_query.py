"""widget_db_query — 查询当前 widget 自己的 store。

LLM-facing schema:
    {
      "table": str,                         # required
      "where": object<string, any>,         # optional
      "limit": int,                         # optional, default 50
      "order_by": str,                      # optional
      "descending": bool                    # optional, default true
    }

只读工具，可并发执行。
"""

from __future__ import annotations

from typing import Any

from ...tool.base_tool import ToolResult, ToolStatus
from .base import WidgetTool


class WidgetDbQueryTool(WidgetTool):
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "Query rows from a table in this widget's local store. "
                "Returns at most `limit` rows (default 50)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name to query.",
                    },
                    "where": {
                        "type": "object",
                        "description": "Equality filter, e.g. {\"status\": \"open\"}.",
                        "additionalProperties": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of rows to return.",
                        "minimum": 1,
                        "default": 50,
                    },
                    "order_by": {
                        "type": "string",
                        "description": "Column name to sort by.",
                    },
                    "descending": {
                        "type": "boolean",
                        "description": "Sort descending (default true).",
                        "default": True,
                    },
                },
                "required": ["table"],
            },
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            store = self._require_store()
        except RuntimeError as e:
            return ToolResult(status=ToolStatus.FAILED, error=str(e))

        table = kwargs.get("table")
        if not isinstance(table, str) or not table:
            return ToolResult(
                status=ToolStatus.FAILED, error="`table` (string) is required"
            )
        where = kwargs.get("where") or None
        if where is not None and not isinstance(where, dict):
            return ToolResult(
                status=ToolStatus.FAILED, error="`where` must be an object/dict"
            )
        limit = kwargs.get("limit", 50)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        order_by = kwargs.get("order_by") or None
        descending = bool(kwargs.get("descending", True))

        try:
            rows = await store.query(
                table=table,
                where=where,
                limit=limit,
                order_by=order_by,
                descending=descending,
            )
        except Exception as e:  # pragma: no cover (storage-specific)
            return ToolResult(status=ToolStatus.FAILED, error=f"query failed: {e}")

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"rows": rows, "count": len(rows)},
            metadata={"table": table, "ps_id": self._ps_id, "widget_id": self._widget_id},
        )
