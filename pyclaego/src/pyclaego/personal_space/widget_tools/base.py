"""Widget 工具基类。

继承 :class:`src.tool.base_tool.BaseTool`，但额外承载 widget 上下文：
- ``store``: 该 widget 的 :class:`WidgetStore`（可能为 ``None``）
- ``widget_id`` / ``ps_id``: 用于日志与归属
- ``emit_fn``: 可选回调，``widget_emit`` 等工具用来对外广播

子类实现 :meth:`execute` 与 :meth:`get_description`；其中
``get_description()`` 必须只暴露 LLM 真正需要的参数，不要泄露
``widget_id`` / ``ps_id`` / 内部句柄等敏感字段。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...tool.base_tool import BaseTool
from ..datastores.base import WidgetStore


class WidgetTool(BaseTool):
    """所有 widget-aware 工具的公共基类。"""

    def __init__(
        self,
        tool_config: dict[str, Any],
        *,
        store: WidgetStore | None,
        ps_id: str,
        widget_id: str,
        emit_fn: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        super().__init__(tool_config)
        self._store: WidgetStore | None = store
        self._ps_id: str = ps_id
        self._widget_id: str = widget_id
        self._emit_fn: Callable[[dict[str, Any]], Any] | None = emit_fn

    # ------------------------------------------------------------------
    # 子类常用 helpers
    # ------------------------------------------------------------------

    def _require_store(self) -> WidgetStore:
        if self._store is None:
            raise RuntimeError(
                f"Widget '{self._widget_id}' has no store configured; "
                f"set widget_config['store'] to enable this tool."
            )
        return self._store

    # ------------------------------------------------------------------
    # BaseTool 抽象方法默认实现
    # ------------------------------------------------------------------

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """Widget 工具默认不接触真实文件系统路径，直接透传。

        如果未来某个 widget 工具开始返回真实文件路径（例如 widget_db_query
        里包含本地附件路径），子类可覆盖此方法做脱敏。
        """
        return raw_output
