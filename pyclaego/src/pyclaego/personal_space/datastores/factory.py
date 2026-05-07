"""WidgetStore 工厂。

读取 ``widget_config["store"]`` 决定实例化哪种实现：

.. code-block:: json

    {
      "store": {
        "type": "sqlite",          // "sqlite" | "jsonl" | "none"
        "schema_file": "schema.sql",
        "db_name": "widget.db"
      }
    }

未配置或 ``type=="none"`` 时返回 ``None``，调用方按"无 store"处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import WidgetStore
from .jsonl_store import JsonlStore
from .sqlite_store import SqliteStore


def create_widget_store(
    widget_config: dict[str, Any],
    workspace_dir: Path,
) -> WidgetStore | None:
    """根据 widget 配置构造合适的 store；不调用 ``open()``。"""
    store_cfg = (widget_config or {}).get("store") or {}
    store_type = (store_cfg.get("type") or "none").lower()

    if store_type in ("none", "off", "disabled", ""):
        return None

    options = {k: v for k, v in store_cfg.items() if k != "type"}

    if store_type == "sqlite":
        return SqliteStore(workspace_dir=workspace_dir, options=options)
    if store_type == "jsonl":
        return JsonlStore(workspace_dir=workspace_dir, options=options)

    raise ValueError(f"Unknown widget store type: {store_type!r}")
