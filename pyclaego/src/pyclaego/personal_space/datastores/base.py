"""WidgetStore 抽象基类。

设计原则：
- 同步/异步友好：所有 IO 方法均为 ``async``；具体实现可在内部用
  ``asyncio.to_thread`` 包裹阻塞 IO（SQLite 走此路径）。
- 表模型可选：JsonlStore 没有"表"概念，将 ``table`` 视为单一逻辑日志名；
  SqliteStore 严格按 ``table`` 路由。调用方约定 ``table`` 为字符串即可。
- 不耦合具体 schema：``schema()`` 返回当前 store 已知的结构信息；调用方/
  LLM 工具据此决定如何构造 query。

最小可用接口：``open / close / write / query / schema``。
``subscribe()`` 占位为 NotImplementedError，留给后续 widget_emit 的事件流。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StoreEvent:
    """store 内部产生的事件（供 ``subscribe`` / ``widget_emit`` 使用）。"""

    kind: str  # "write" | "delete" | "schema_change" | ...
    table: str
    payload: dict[str, Any] = field(default_factory=dict)


class WidgetStore(ABC):
    """所有 Widget 存储实现的公共接口。

    生命周期：
    1. ``__init__`` 接受 ``workspace_dir`` 与可选的 ``options``（来自
       ``widget_config["store"]``）；不应在 ``__init__`` 里做 IO。
    2. ``open()`` 完成实际打开/初始化（建表、确保文件存在等）。
    3. 期间任意调用 ``write`` / ``query`` / ``schema``。
    4. ``close()`` 释放所有资源；幂等。
    """

    def __init__(
        self,
        *,
        workspace_dir: Path,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.workspace_dir: Path = Path(workspace_dir).resolve()
        self.options: dict[str, Any] = dict(options or {})
        self._opened: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @abstractmethod
    async def open(self) -> None:
        """打开/初始化 store（建表、确保文件存在等）。幂等。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭 store，释放句柄/连接。幂等。"""

    @property
    def is_open(self) -> bool:
        return self._opened

    # ------------------------------------------------------------------
    # 数据操作
    # ------------------------------------------------------------------

    @abstractmethod
    async def write(self, table: str, rows: list[dict[str, Any]]) -> int:
        """追加写入若干行；返回成功写入的行数。"""

    @abstractmethod
    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        """按等值条件查询；具体实现可扩展更丰富的过滤。

        Args:
            table: 表名 / 日志通道名
            where: 等值过滤；仅支持 ``{col: value}``
            limit: 最多返回多少行（默认全部）
            order_by: 排序字段名；JsonlStore 仅支持 "_ts"
            descending: 倒序返回（默认 True，最新在前）
        """

    @abstractmethod
    async def schema(self) -> dict[str, Any]:
        """返回当前 store 的结构描述（实现各异）。"""

    # ------------------------------------------------------------------
    # 事件流（占位）
    # ------------------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[StoreEvent]:  # pragma: no cover
        """订阅 store 内部事件流。当前实现未启用。"""
        raise NotImplementedError(
            "subscribe() is reserved for a future widget_emit fan-out."
        )
        # 让类型检查认为这是 async generator
        if False:
            yield  # type: ignore[unreachable]
