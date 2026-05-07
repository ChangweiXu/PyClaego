"""widget_tools.builder — 根据 widget_config 构造工具集合。

约定 ``widget_config["widget_tools"]`` 形如：

.. code-block:: json

    {
      "widget_tools": {
        "widget_db_query": {"enabled": true},
        "widget_db_write": {"enabled": true},
        "widget_emit":     {"enabled": true}
      }
    }

未声明的工具默认按 *启用* 处理（MVP；后续可改为白名单）；
``enabled: false`` 显式禁用。

仅当 widget 拥有 store 时构造 db 工具；``widget_emit`` 始终可用。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..datastores.base import WidgetStore
from .base import WidgetTool
from .widget_db_query import WidgetDbQueryTool
from .widget_db_write import WidgetDbWriteTool
from .widget_emit import WidgetEmitTool

_DEFAULT_TOOL_NAMES = ("widget_db_query", "widget_db_write", "widget_emit")


def _is_enabled(tools_cfg: dict[str, Any], name: str) -> bool:
    spec = tools_cfg.get(name)
    if spec is None:
        return True
    if isinstance(spec, bool):
        return spec
    if isinstance(spec, dict):
        return bool(spec.get("enabled", True))
    return True


def _tool_config_for(tools_cfg: dict[str, Any], name: str) -> dict[str, Any]:
    spec = tools_cfg.get(name) or {}
    if not isinstance(spec, dict):
        spec = {}
    return {
        "tool_type": name,
        "tool_name": spec.get("tool_name", name),
        "enabled": _is_enabled(tools_cfg, name),
        "timeout": spec.get("timeout", 30),
    }


def build_widget_tools(
    *,
    widget_config: dict[str, Any],
    store: WidgetStore | None,
    ps_id: str,
    widget_id: str,
    emit_fn: Callable[[dict[str, Any]], Any] | None = None,
) -> list[WidgetTool]:
    """构造该 widget 的工具实例列表。"""
    tools_cfg = (widget_config or {}).get("widget_tools") or {}
    tools: list[WidgetTool] = []

    # widget_db_query / write 仅在有 store 时才有意义
    if store is not None:
        if _is_enabled(tools_cfg, "widget_db_query"):
            tools.append(
                WidgetDbQueryTool(
                    _tool_config_for(tools_cfg, "widget_db_query"),
                    store=store,
                    ps_id=ps_id,
                    widget_id=widget_id,
                    emit_fn=emit_fn,
                )
            )
        if _is_enabled(tools_cfg, "widget_db_write"):
            tools.append(
                WidgetDbWriteTool(
                    _tool_config_for(tools_cfg, "widget_db_write"),
                    store=store,
                    ps_id=ps_id,
                    widget_id=widget_id,
                    emit_fn=emit_fn,
                )
            )

    if _is_enabled(tools_cfg, "widget_emit"):
        tools.append(
            WidgetEmitTool(
                _tool_config_for(tools_cfg, "widget_emit"),
                store=store,
                ps_id=ps_id,
                widget_id=widget_id,
                emit_fn=emit_fn,
            )
        )

    # 拒绝未知工具（防止配置 typo 静默通过）
    unknown = [k for k in tools_cfg.keys() if k not in _DEFAULT_TOOL_NAMES]
    if unknown:
        raise ValueError(
            f"Unknown widget tool(s) in widget_config['tools']: {unknown!r}"
        )

    return tools
