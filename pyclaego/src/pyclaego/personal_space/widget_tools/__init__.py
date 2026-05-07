"""Widget-aware tools — Phase 3.4。

每个 Widget 在 ``load()`` 阶段构造一组绑定到本 widget 的工具实例
（``store`` / ``widget_id`` / ``ps_id`` 等通过 DI 注入），
并通过 :meth:`Agent.inject_widget_tools` 交给 agent。

LLM-facing schema (``get_description()``) 不暴露 widget_id / ps_id 等内部字段，
仅暴露真正面向模型的参数（如 table / row / query 等）。
"""

from __future__ import annotations

from .base import WidgetTool
from .builder import build_widget_tools
from .widget_db_query import WidgetDbQueryTool
from .widget_db_write import WidgetDbWriteTool
from .widget_emit import WidgetEmitTool

__all__ = [
    "WidgetDbQueryTool",
    "WidgetDbWriteTool",
    "WidgetEmitTool",
    "WidgetTool",
    "build_widget_tools",
]
