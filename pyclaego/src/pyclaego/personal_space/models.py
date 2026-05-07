"""PersonalSpace 模型的数据类型。

仅承载磁盘上的 manifest 形态（``personal_space.json`` /
``widget.json``），不掺入任何运行时状态 — 那些在 ``PersonalSpace``
与未来的 ``Widget`` 类里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WidgetLayout:
    """Dashboard 上 widget 的网格位置。"""

    x: int = 0
    y: int = 0
    w: int = 4
    h: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> WidgetLayout:
        if not d:
            return cls()
        return cls(
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", 4)),
            h=int(d.get("h", 3)),
        )


@dataclass
class WidgetManifest:
    """磁盘上 ``widget.json`` 的内容（不含 ``widget.config.json`` — 那是配置层）。"""

    widget_id: str
    widget_class: str
    title: str = ""
    layout: WidgetLayout = field(default_factory=WidgetLayout)
    cron: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "widget_id": self.widget_id,
            "widget_class": self.widget_class,
            "title": self.title,
            "layout": self.layout.to_dict(),
            "cron": list(self.cron),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WidgetManifest:
        return cls(
            widget_id=str(d["widget_id"]),
            widget_class=str(d["widget_class"]),
            title=str(d.get("title", "")),
            layout=WidgetLayout.from_dict(d.get("layout")),
            cron=list(d.get("cron", []) or []),
            metadata=dict(d.get("metadata", {}) or {}),
        )


@dataclass
class PSManifest:
    """磁盘上 ``personal_space.json`` 的内容。"""

    ps_id: str
    title: str = ""
    description: str = ""
    widget_order: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = "generic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ps_id": self.ps_id,
            "title": self.title,
            "description": self.description,
            "widget_order": list(self.widget_order),
            "metadata": dict(self.metadata),
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PSManifest:
        return cls(
            ps_id=str(d["ps_id"]),
            title=str(d.get("title", "")),
            description=str(d.get("description", "")),
            widget_order=list(d.get("widget_order", []) or []),
            metadata=dict(d.get("metadata", {}) or {}),
            kind=str(d.get("kind", "generic")),
        )


__all__ = ["PSManifest", "WidgetLayout", "WidgetManifest"]
