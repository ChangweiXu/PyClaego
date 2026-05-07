"""WidgetClass 数据结构。

``WidgetClassSpec`` 是磁盘上 ``widget_class.json`` 的不可变运行时投影。
它额外承载一些按规范应支持的可选字段：

- ``schema_file``：声明式提示，告诉 SqliteStore 去哪里取建表语句。
- ``default_viewers_file`` / ``default_cron_file``：UI 默认 viewer / cron 配置
  的相对路径（相对 ``asset_dir``）。
- ``hook_class``：可选 ``WidgetHook`` 子类（来自同目录下 ``widget_class.py``，
  Phase 11 — 调用点已预留，目前是 no-op）。

``defaults`` 中如果出现 ``store.schema_file``，会在加载时被解析为绝对路径，
让 ``SqliteStore`` 不必重新猜测相对位置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...logging import get_running_log

_rlog = get_running_log()


@dataclass
class WidgetClassSpec:
    class_id: str
    title: str
    description: str
    defaults: dict[str, Any]
    config_schema: dict[str, Any]
    asset_dir: Path
    source: str = "builtin"
    schema_file: str | None = None
    default_viewers_file: str | None = None
    default_cron_file: str | None = None
    hook_class: type[Any] | None = field(default=None, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        asset_dir: Path,
        source: str = "builtin",
    ) -> WidgetClassSpec:
        if not isinstance(data, dict):
            raise TypeError(
                f"widget_class.json 必须是 JSON 对象，实际: {type(data)}"
            )
        class_id = data.get("class_id") or data.get("id")
        if not class_id or not isinstance(class_id, str):
            raise ValueError(f"widget_class.json 缺少合法的 class_id: {data}")

        asset_dir = Path(asset_dir).resolve()

        defaults = dict(data.get("defaults") or data.get("session_defaults") or {})
        # 把 defaults.store.schema_file 解析为相对 asset_dir 的绝对路径。
        # 这让用户可以在 widget_class.json 里写 "schema.sql"，
        # 实际 SqliteStore 读到的就是 <asset_dir>/schema.sql。
        store_block = defaults.get("store")
        if isinstance(store_block, dict):
            sf = store_block.get("schema_file")
            if isinstance(sf, str) and sf:
                p = Path(sf)
                if not p.is_absolute():
                    p = (asset_dir / p).resolve()
                store_block["schema_file"] = str(p)

        return cls(
            class_id=class_id,
            title=str(data.get("title") or class_id),
            description=str(data.get("description") or ""),
            defaults=defaults,
            config_schema=dict(data.get("config_schema") or {}),
            asset_dir=asset_dir,
            source=source,
            schema_file=data.get("schema_file"),
            default_viewers_file=data.get("default_viewers_file"),
            default_cron_file=data.get("default_cron_file"),
            hook_class=None,
            raw=data,
        )

    def resolve_asset(self, rel: str) -> Path:
        """把一个相对 asset_dir 的资源路径解析成绝对路径。"""
        p = Path(rel)
        if p.is_absolute():
            return p
        return (self.asset_dir / p).resolve()

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "title": self.title,
            "description": self.description,
            "defaults": dict(self.defaults),
            "config_schema": dict(self.config_schema),
            "asset_dir": str(self.asset_dir),
            "source": self.source,
            "schema_file": self.schema_file,
            "default_viewers_file": self.default_viewers_file,
            "default_cron_file": self.default_cron_file,
            "has_hook": self.hook_class is not None,
        }


__all__ = ["WidgetClassSpec"]
