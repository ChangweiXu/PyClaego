"""极简 prompt 模板渲染。

故意不强依赖 jinja2：cron prompt 实际很短，``str.format`` 足以覆盖大多数场景。
未知占位符会被原样保留（``{foo}`` 不会抛 KeyError）。
"""

from __future__ import annotations

import datetime as _dt
import string
from typing import Any


class _SafeDict(dict):
    """用于 ``str.format_map``：未知键返回 ``{key}`` 原样。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_prompt(template: str, params: dict[str, Any]) -> str:
    """按 ``{key}`` 占位符渲染。自动注入 ``now`` / ``today`` 两个常用变量。"""
    if not template:
        return ""
    now = _dt.datetime.now()
    base: dict[str, Any] = {
        "now": now.isoformat(timespec="seconds"),
        "today": now.date().isoformat(),
        "year": now.year,
        "month": now.month,
        "day": now.day,
    }
    base.update(params or {})
    try:
        return string.Formatter().vformat(template, (), _SafeDict(base))
    except Exception:
        # 渲染失败时回退到原模板，避免 cron 直接挂掉
        return template


__all__ = ["render_prompt"]
