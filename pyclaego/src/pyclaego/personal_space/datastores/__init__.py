"""WidgetStore — 持久化数据层（Phase 3.3）

每个 Widget 拥有独立的 ``WidgetStore`` 实例，存储在
``widget.workspace_dir / "data" /``  下。

支持两种实现：
- :class:`SqliteStore` — 结构化查询，支持 schema_file 自定义建表语句
- :class:`JsonlStore`  — 追加写日志，扫描式查询，零依赖、零迁移成本

所有实现共享 :class:`WidgetStore` 协议；调用方使用统一接口
``open / close / write / query / schema``。
"""

from __future__ import annotations

from .base import StoreEvent, WidgetStore
from .factory import create_widget_store
from .jsonl_store import JsonlStore
from .sqlite_store import SqliteStore

__all__ = [
    "JsonlStore",
    "SqliteStore",
    "StoreEvent",
    "WidgetStore",
    "create_widget_store",
]
