"""PersonalSpaceConfigManager — 单个 PS 的配置管理器。

层级（从低到高）：

    global   ← 全局 YAML（``ConfigManager`` 单例）
    ps       ← ``personal_spaces/<ps_id>/personal_space.config.json``
    class    ← ``WidgetClass`` defaults（按需在 ``resolve_widget`` 时合入）
    widget   ← ``personal_spaces/<ps_id>/widgets/<wid>/widget.config.json``

合并方式：``resolver.deep_merge``（key 级覆盖，dict 内递归）。

热重载：使用 ``watchfiles`` 监听 PS 目录树。变更时重读相关文件、清理缓存、
触发已注册的 ``ConfigChanged`` 订阅者回调。

使用示例::

    cfg = PersonalSpaceConfigManager(Path("/data/personal_spaces/alice"))
    cfg.start_watching()
    cfg.subscribe(lambda scope, payload: print("changed", scope))
    runtime = cfg.resolve_widget("w_chat_1")  # 完整解析后的 widget 运行时配置
    cfg.stop_watching()
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from .json_loader import load_json_file, resolve_tree
from .manager import get_config
from .resolver import deep_merge

try:
    from watchfiles import awatch  # type: ignore
    _HAS_WATCHFILES = True
except Exception:  # pragma: no cover
    _HAS_WATCHFILES = False


# scope: ("ps",) | ("ps_config",) | ("widget", widget_id) | ("widget_config", widget_id)
ConfigScope = tuple[str, ...]
ChangeCallback = Callable[[ConfigScope, Any], None]


class PersonalSpaceConfigManager:
    """每个 PS 一个实例。线程安全（读写都加同一把锁）。"""

    PERSONAL_SPACE_FILE = "personal_space.json"
    PERSONAL_SPACE_CONFIG_FILE = "personal_space.config.json"
    WIDGETS_DIR = "widgets"
    WIDGET_FILE = "widget.json"
    WIDGET_CONFIG_FILE = "widget.config.json"

    def __init__(
        self,
        ps_root: Path,
        global_config_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        """
        Args:
            ps_root: ``.../personal_spaces/<ps_id>/`` 的目录路径
            global_config_provider: 返回当前全局配置 dict 的可调用对象。
                典型是 ``lambda: ConfigManager().config``。``None`` 表示不合并全局层。
        """
        self.ps_root = Path(ps_root).resolve()
        self._global_provider = global_config_provider

        self._lock = threading.RLock()
        self._ps_manifest: dict[str, Any] | None = None  # personal_space.json (raw)
        self._ps_config_raw: dict[str, Any] | None = None
        self._widget_manifests: dict[str, dict[str, Any]] = {}
        self._widget_configs_raw: dict[str, dict[str, Any]] = {}

        # 解析后的缓存（按 widget_id 维度；None 键 = PS 级整体配置）
        self._resolved_cache: dict[str | None, dict[str, Any]] = {}

        self._subscribers: list[ChangeCallback] = []

        # watchfiles 句柄
        self._watch_task: asyncio.Task | None = None
        self._watch_stop: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------

    @property
    def ps_id(self) -> str:
        return self.ps_root.name

    def _ps_manifest_path(self) -> Path:
        return self.ps_root / self.PERSONAL_SPACE_FILE

    def _ps_config_path(self) -> Path:
        return self.ps_root / self.PERSONAL_SPACE_CONFIG_FILE

    def _widgets_root(self) -> Path:
        return self.ps_root / self.WIDGETS_DIR

    def _widget_manifest_path(self, widget_id: str) -> Path:
        return self._widgets_root() / widget_id / self.WIDGET_FILE

    def _widget_config_path(self, widget_id: str) -> Path:
        return self._widgets_root() / widget_id / self.WIDGET_CONFIG_FILE

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load(self) -> None:
        """首次加载所有 PS 级文件 + 已存在的 widget 配置。"""
        with self._lock:
            self._ps_manifest = self._safe_load(self._ps_manifest_path())
            self._ps_config_raw = self._safe_load(self._ps_config_path()) or {}

            self._widget_manifests.clear()
            self._widget_configs_raw.clear()
            widgets_root = self._widgets_root()
            if widgets_root.is_dir():
                for entry in widgets_root.iterdir():
                    if not entry.is_dir():
                        continue
                    wid = entry.name
                    m = self._safe_load(self._widget_manifest_path(wid))
                    if m is not None:
                        self._widget_manifests[wid] = m
                    c = self._safe_load(self._widget_config_path(wid))
                    if c is not None:
                        self._widget_configs_raw[wid] = c

            self._resolved_cache.clear()

    def _safe_load(self, path: Path) -> dict[str, Any] | None:
        try:
            return load_json_file(path)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件 {path} JSON 解析失败: {e}") from e

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def _global_layer(self) -> dict[str, Any]:
        if self._global_provider is None:
            return {}
        try:
            g = self._global_provider() or {}
        except Exception:
            return {}
        # 全局配置已由 ConfigManager 解析过了，直接 deep-copy 防止互相污染
        return deepcopy(g) if isinstance(g, dict) else {}

    def resolve_ps(self) -> dict[str, Any]:
        """返回 PS 整体运行时配置（不含某个具体 widget 的覆盖）。"""
        with self._lock:
            if None in self._resolved_cache:
                return deepcopy(self._resolved_cache[None])
            merged = deep_merge(
                self._global_layer(),
                deepcopy(self._ps_config_raw or {}),
            )
            resolved = resolve_tree(merged, base_dir=self.ps_root)
            self._resolved_cache[None] = resolved
            return deepcopy(resolved)

    def resolve_widget(
        self,
        widget_id: str,
        widget_class_defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回某个 widget 的完整运行时配置。

        Args:
            widget_id: widget 目录名
            widget_class_defaults: 该 widget 所属 ``WidgetClass`` 的默认配置。
                由调用方（``WidgetFactory``）从 ``widget_class.json`` 提供，
                这样 ``PersonalSpaceConfigManager`` 不需要感知 ``WidgetClass`` 注册表。
        """
        with self._lock:
            cache_key = widget_id  # widget_class_defaults 不参与 cache，因为它是外部输入
            if cache_key in self._resolved_cache and widget_class_defaults is None:
                return deepcopy(self._resolved_cache[cache_key])

            widget_config = self._widget_configs_raw.get(widget_id, {})
            merged = deep_merge(
                self._global_layer(),
                deepcopy(self._ps_config_raw or {}),
                deepcopy(widget_class_defaults or {}),
                deepcopy(widget_config),
            )
            base_dir = self._widgets_root() / widget_id
            if not base_dir.is_dir():
                base_dir = self.ps_root
            resolved = resolve_tree(merged, base_dir=base_dir)
            if widget_class_defaults is None:
                self._resolved_cache[cache_key] = resolved
            return deepcopy(resolved)

    def get_ps_manifest(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._ps_manifest or {})

    def get_widget_manifest(self, widget_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._widget_manifests.get(widget_id, {}))

    def get_widget_config_raw(self, widget_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._widget_configs_raw.get(widget_id, {}))

    def list_widget_ids(self) -> list[str]:
        with self._lock:
            return sorted(set(self._widget_manifests) | set(self._widget_configs_raw))

    # ------------------------------------------------------------------
    # 写回（供 Web UI 用）
    # ------------------------------------------------------------------

    def write_ps_config(self, new_config: dict[str, Any]) -> None:
        path = self._ps_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)
        self.reload_file(path)

    def write_widget_config(self, widget_id: str, new_config: dict[str, Any]) -> None:
        path = self._widget_config_path(widget_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)
        self.reload_file(path)

    def reload_file(self, path: Path) -> None:
        """单文件重新加载并清理相关缓存。"""
        path = Path(path).resolve()
        with self._lock:
            scope: ConfigScope
            payload: Any = None
            if path == self._ps_config_path().resolve():
                self._ps_config_raw = self._safe_load(path) or {}
                self._resolved_cache.clear()
                scope = ("ps_config",)
                payload = self._ps_config_raw
            elif path == self._ps_manifest_path().resolve():
                self._ps_manifest = self._safe_load(path)
                scope = ("ps",)
                payload = self._ps_manifest
            else:
                # widget 文件
                rel = path.relative_to(self._widgets_root()) if self._widgets_root() in path.parents else None
                if rel is None or len(rel.parts) < 2:
                    return
                wid = rel.parts[0]
                fname = rel.parts[-1]
                if fname == self.WIDGET_CONFIG_FILE:
                    self._widget_configs_raw[wid] = self._safe_load(path) or {}
                    self._resolved_cache.pop(wid, None)
                    scope = ("widget_config", wid)
                    payload = self._widget_configs_raw[wid]
                elif fname == self.WIDGET_FILE:
                    self._widget_manifests[wid] = self._safe_load(path) or {}
                    scope = ("widget", wid)
                    payload = self._widget_manifests[wid]
                else:
                    return
        self._notify(scope, payload)

    # ------------------------------------------------------------------
    # 订阅 / 热重载
    # ------------------------------------------------------------------

    def subscribe(self, callback: ChangeCallback) -> Callable[[], None]:
        """注册变更回调，返回退订函数。"""
        with self._lock:
            self._subscribers.append(callback)
        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return _unsubscribe

    def _notify(self, scope: ConfigScope, payload: Any) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(scope, payload)
            except Exception:
                # 不让单个订阅者的错误打断热重载循环
                import traceback
                traceback.print_exc()

    async def start_watching(self) -> None:
        """在当前事件循环里启动 watchfiles 监听任务。"""
        if not _HAS_WATCHFILES:
            return
        if self._watch_task is not None:
            return
        self._watch_stop = asyncio.Event()
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop_watching(self) -> None:
        if self._watch_task is None:
            return
        if self._watch_stop is not None:
            self._watch_stop.set()
        self._watch_task.cancel()
        try:
            await self._watch_task
        except (asyncio.CancelledError, Exception):
            pass
        self._watch_task = None
        self._watch_stop = None

    async def _watch_loop(self) -> None:  # pragma: no cover (集成测试覆盖)
        assert self._watch_stop is not None
        try:
            async for changes in awatch(str(self.ps_root), stop_event=self._watch_stop):
                for _change_kind, raw_path in changes:
                    p = Path(raw_path)
                    if p.suffix != ".json":
                        continue
                    try:
                        self.reload_file(p)
                    except Exception:
                        import traceback
                        traceback.print_exc()
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# Public factory functions — external modules must use these; never import
# PersonalSpaceConfigManager or ConfigManager directly.
# ---------------------------------------------------------------------------


def get_ps_config(
    ps_root: Path,
    global_config_provider: Callable[[], dict[str, Any]] | None = None,
) -> PersonalSpaceConfigManager:
    """Create a PersonalSpaceConfigManager wired to the global config singleton.

    Args:
        ps_root: Path to the personal space root directory.
        global_config_provider: Override the config provider (mainly for tests).
            Defaults to ``lambda: get_config().config``.

    Returns:
        A PersonalSpaceConfigManager ready to call ``load()`` / ``resolve_*()`` on.
    """
    provider = global_config_provider or (lambda: get_config().config)
    return PersonalSpaceConfigManager(ps_root, global_config_provider=provider)


def get_ps_widget_config(
    ps_root: Path,
    widget_id: str,
    widget_class_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fully resolved config dict for a single widget (one-shot).

    Creates a transient PersonalSpaceConfigManager, loads all files, and returns
    the resolved widget config.  The manager is not retained — callers that need
    hot-reload or subscription should use ``get_ps_config`` directly.

    Args:
        ps_root: Path to the personal space root directory.
        widget_id: Widget directory name inside ``<ps_root>/widgets/``.
        widget_class_defaults: Optional WidgetClass-level defaults to include in
            the merge stack (provided by WidgetFactory).

    Returns:
        Fully resolved config dict (all tags, env-vars, and references expanded).
    """
    mgr = get_ps_config(ps_root)
    mgr.load()
    return mgr.resolve_widget(widget_id, widget_class_defaults)


__all__ = [
    "PersonalSpaceConfigManager",
    "get_ps_config",
    "get_ps_widget_config",
]
