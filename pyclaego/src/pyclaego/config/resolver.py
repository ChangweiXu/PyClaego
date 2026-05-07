"""Deep-merge resolver for layered configs.

PersonalSpace 模型的配置解析采用 **逐层深合并**（不是 group-level overwrite）：

    global  ←  personal_space.config  ←  widget_class.defaults  ←  widget.config

每一层都可以只覆盖具体的 key，而不是替换整个 group。

本模块提供 stateless 的 ``deep_merge`` 与 ``resolve_layers``，
被 ``PersonalSpaceConfigManager`` 与未来的 ``WidgetClass`` 加载器共享。
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any


def deep_merge(*layers: dict[str, Any]) -> dict[str, Any]:
    """递归深合并多层配置（后者覆盖前者）。

    规则：
    - dict + dict  → 递归合并
    - 任何其他类型 → 后者直接覆盖前者（不做合并）
    - list 不做拼接，直接整体替换（避免对 viewers/cron 这类有序数组产生意外）

    None / 空 dict 层会被跳过。返回结果是新对象（不修改输入）。
    """
    out: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        if not isinstance(layer, dict):
            raise TypeError(
                f"deep_merge layers must be dicts, got {type(layer).__name__}"
            )
        _merge_into(out, layer)
    return out


def _merge_into(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if (
            key in dst
            and isinstance(dst[key], dict)
            and isinstance(value, dict)
        ):
            _merge_into(dst[key], value)
        else:
            dst[key] = deepcopy(value)


def resolve_layers(layers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """``deep_merge`` 的 list 形式入口（便于编程式构造层序）。"""
    return deep_merge(*list(layers))


__all__ = ["deep_merge", "resolve_layers"]
