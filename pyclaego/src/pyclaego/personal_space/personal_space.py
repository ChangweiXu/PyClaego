"""``PersonalSpace`` 运行时（Phase 1.1 骨架）。

一个 PS 拥有：
- 一份 ``PSManifest``（``personal_space.json``）
- 一个 ``PersonalSpaceConfigManager``（per-PS 层叠配置）
- ``dict[widget_id, Widget]`` — 运行时 widget 实例
- ``active_connections: set[str]`` — 用于 LRU/idle 卸载计数
- ``in_flight_tasks: int`` — 用于守护卸载（外部计数器）

外部世界与 PS 的所有交互都通过 ``PersonalSpaceManager``，
本类不关心 ``CoreScheduler`` / 网络协议。

Phase 1.1 的限制：
- ``Widget`` 只是占位实现，``process_message`` 抛 ``NotImplementedError``
- 不调用真正的 ``WidgetClassRegistry``；自动配置仅创建空配置文件
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ..config import PersonalSpaceConfigManager
from ..logging import get_running_log
from .models import PSManifest, WidgetManifest
from .widget import Widget
from .widget_classes import WidgetClassRegistry

_rlog = get_running_log()


# 自动注入到新 PS 中的默认 widget — 真正的 WidgetClass 集成在 Phase 3.5
DEFAULT_WIDGET_ID = "w_chat_default"
DEFAULT_WIDGET_CLASS = "chat"
DEFAULT_WIDGET_TITLE = "Chat"

# PS kind 常量
KIND_GENERIC = "generic"
KIND_FEISHU_CHAT = "feishu_chat"


class PersonalSpace:
    """单个 PersonalSpace 的运行时实例。"""

    def __init__(
        self,
        ps_id: str,
        ps_root: Path,
        config_manager: PersonalSpaceConfigManager,
        *,
        widget_class_registry: WidgetClassRegistry | None = None,
        widget_factory: Any | None = None,
    ) -> None:
        self.ps_id: str = ps_id
        self.ps_root: Path = Path(ps_root).resolve()
        self.config: PersonalSpaceConfigManager = config_manager
        self._widget_class_registry: WidgetClassRegistry = (
            widget_class_registry or WidgetClassRegistry.instance()
        )
        # widget_factory(ps_id, manifest, workspace_dir, resolved_config) -> Widget
        # 用于测试中替换默认的 Widget 构造，注入 mock agent/context factory。
        self._widget_factory: Any | None = widget_factory

        self._widgets: dict[str, Widget] = {}
        self._widget_lock = asyncio.Lock()

        self._active_connections: set[str] = set()
        self._in_flight_tasks: int = 0
        self._last_activity_ts: float = time.time()

        # ConfigManager.subscribe 退订函数
        self._unsubscribe_config: Any | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """初次加载（由 PersonalSpaceManager 调用）。

        - 加载 PSConfigManager（已读盘）
        - 不会主动构造 Widget；``get_widget`` 按需懒加载
        - 注册配置变更订阅（用于将来转发到 widget）
        """
        # PSConfigManager.load() 是同步的
        self.config.load()
        # 启动文件监听（如果调用方未自行启动）
        try:
            await self.config.start_watching()
        except Exception:
            _rlog.exception("core_service", f"[PS:{self.ps_id}] start_watching 失败（继续）")
        # 配置变更订阅 — Phase 1.1 仅清空已加载的 widget 缓存以便重新解析
        self._unsubscribe_config = self.config.subscribe(self._on_config_changed)

    async def unload(self) -> None:
        """资源回收，幂等。"""
        # 停止文件监听
        try:
            await self.config.stop_watching()
        except Exception:
            _rlog.exception("core_service", f"[PS:{self.ps_id}] stop_watching 失败（继续）")
        # 退订
        if self._unsubscribe_config is not None:
            try:
                self._unsubscribe_config()
            except Exception:
                pass
            self._unsubscribe_config = None
        # 卸载 widgets
        async with self._widget_lock:
            widgets = list(self._widgets.values())
            self._widgets.clear()
        for w in widgets:
            try:
                await w.unload()
            except Exception:
                _rlog.exception("core_service", f"[PS:{self.ps_id}] widget {w.widget_id} unload 失败")

    def _on_config_changed(self, scope, payload) -> None:
        """配置变更回调：清掉相关 widget 缓存。

        策略：
        - ``("widget_config", wid)`` / ``("widget", wid)`` → 仅清掉对应 widget；
        - ``("ps_config",)`` / ``("ps",)`` → 清掉**所有** widget（PS 级配置参与
          ``resolve_widget`` 的 deep_merge，影响每个 widget）。

        被清掉的 Widget 实例不会主动 ``unload()``：旧实例可能仍被在飞任务持有，
        我们只是从缓存里弹出，下一次 ``get_widget()`` 会按最新配置重建。
        弹出的旧实例会随 in-flight task 完成后被 GC 回收。
        """
        _rlog.info("core_service", f"[PS:{self.ps_id}] config changed: {scope}")
        if not scope:
            return
        head = scope[0]
        if head in ("widget_config", "widget") and len(scope) >= 2:
            wid = scope[1]
            self._widgets.pop(wid, None)
        elif head in ("ps_config", "ps"):
            # PS 级变更 → 所有 widget 的 resolved_config 都会变
            if self._widgets:
                _rlog.info(
                    "core_service",
                    f"[PS:{self.ps_id}] PS-level config changed → 清空 {len(self._widgets)} 个已加载 widget 缓存",
                )
            self._widgets.clear()

    # ------------------------------------------------------------------
    # 连接计数
    # ------------------------------------------------------------------

    def open_connection(self, conn_id: str) -> None:
        self._active_connections.add(conn_id)
        self._touch()

    def close_connection(self, conn_id: str) -> None:
        self._active_connections.discard(conn_id)
        self._touch()

    @property
    def active_connection_count(self) -> int:
        return len(self._active_connections)

    @property
    def in_flight_tasks(self) -> int:
        return self._in_flight_tasks

    def inc_in_flight(self) -> None:
        self._in_flight_tasks += 1
        self._touch()

    def dec_in_flight(self) -> None:
        if self._in_flight_tasks > 0:
            self._in_flight_tasks -= 1
        self._touch()

    @property
    def last_activity_ts(self) -> float:
        return self._last_activity_ts

    def _touch(self) -> None:
        self._last_activity_ts = time.time()

    def is_idle(self) -> bool:
        return self._active_connections == set() and self._in_flight_tasks == 0

    # ------------------------------------------------------------------
    # Widget 访问
    # ------------------------------------------------------------------

    def get_manifest(self) -> PSManifest:
        raw = self.config.get_ps_manifest()
        if not raw:
            return PSManifest(ps_id=self.ps_id)
        return PSManifest.from_dict(raw)

    @property
    def kind(self) -> str:
        return self.get_manifest().kind

    def list_widget_ids(self) -> list[str]:
        return self.config.list_widget_ids()

    async def get_widget(self, widget_id: str) -> Widget:
        """按需加载 widget 实例（含 ``Widget.load()``）。"""
        async with self._widget_lock:
            if widget_id in self._widgets:
                return self._widgets[widget_id]
            manifest_raw = self.config.get_widget_manifest(widget_id)
            if not manifest_raw:
                raise KeyError(f"Widget {widget_id} 在 PS {self.ps_id} 中不存在")
            manifest = WidgetManifest.from_dict(manifest_raw)
            workspace_dir = (self.ps_root / "widgets" / widget_id).resolve()
            workspace_dir.mkdir(parents=True, exist_ok=True)
            class_defaults = self._widget_class_registry.get_defaults(
                manifest.widget_class
            )
            # 可选 spec：主要为了 hook_class。未注册的 class 返回 None，
            # Widget 里会容错。
            class_spec: Any | None = None
            try:
                if self._widget_class_registry.has(manifest.widget_class):
                    class_spec = self._widget_class_registry.get(manifest.widget_class)
            except Exception:
                class_spec = None
            resolved = self.config.resolve_widget(
                widget_id, widget_class_defaults=class_defaults
            )
            if self._widget_factory is not None:
                widget = self._widget_factory(
                    self.ps_id, manifest, workspace_dir, resolved
                )
            else:
                widget = Widget(
                    ps_id=self.ps_id,
                    manifest=manifest,
                    workspace_dir=workspace_dir,
                    resolved_config=resolved,
                    widget_class_spec=class_spec,
                )
            try:
                await widget.load()
            except Exception:
                # load 失败不缓存，避免下次 get 拿到坏实例
                _rlog.exception(
                    "core_service",
                    f"[PS:{self.ps_id}] widget {widget_id} load 失败",
                )
                raise
            self._widgets[widget_id] = widget
            return widget

    # ------------------------------------------------------------------
    # 引导（首次创建 PS 时调用，非常薄）
    # ------------------------------------------------------------------

    @classmethod
    def bootstrap_on_disk(cls, ps_root: Path, ps_id: str, *, kind: str = KIND_GENERIC) -> None:
        """在磁盘上为新 PS 创建最小目录结构与默认 chat widget。

        幂等：已有文件不会被覆盖。
        """
        ps_root = Path(ps_root)
        ps_root.mkdir(parents=True, exist_ok=True)

        manifest_path = ps_root / "personal_space.json"
        if not manifest_path.exists():
            manifest = PSManifest(
                ps_id=ps_id,
                title=ps_id,
                description="",
                widget_order=[DEFAULT_WIDGET_ID],
                kind=kind,
            )
            manifest_path.write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        ps_config_path = ps_root / "personal_space.config.json"
        if not ps_config_path.exists():
            ps_config_path.write_text("{}\n", encoding="utf-8")

        # 默认 chat widget
        widget_dir = ps_root / "widgets" / DEFAULT_WIDGET_ID
        widget_dir.mkdir(parents=True, exist_ok=True)
        wm_path = widget_dir / "widget.json"
        if not wm_path.exists():
            wm = WidgetManifest(
                widget_id=DEFAULT_WIDGET_ID,
                widget_class=DEFAULT_WIDGET_CLASS,
                title=DEFAULT_WIDGET_TITLE,
            )
            wm_path.write_text(
                json.dumps(wm.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        wc_path = widget_dir / "widget.config.json"
        if not wc_path.exists():
            wc_path.write_text("{}\n", encoding="utf-8")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PersonalSpace id={self.ps_id} "
            f"conns={len(self._active_connections)} "
            f"widgets={len(self._widgets)}>"
        )


__all__ = [
    "DEFAULT_WIDGET_CLASS",
    "DEFAULT_WIDGET_ID",
    "KIND_FEISHU_CHAT",
    "KIND_GENERIC",
    "PersonalSpace",
]
