"""``WidgetClassRegistry`` —— 全局 WidgetClass 注册表。

Builtin widget classes 位于 ``src/personal_space/widget_classes/widgets/`` 下，
每个子目录一个 class。

API 极简：``get(class_id)`` / ``list()`` / ``get_defaults(class_id)``。
"""

from __future__ import annotations

import builtins
import json
import threading
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from .hook import WidgetHook
from .spec import WidgetClassSpec
from .widgets.notes.widget_class import WidgetHook as _NotesHook

_rlog = get_running_log()

# Pre-registered builtin hooks — add an entry here when a new builtin
# widget class provides a widget_class.py hook.
_BUILTIN_HOOKS: dict[str, type[WidgetHook]] = {
    "notes": _NotesHook,
}

# 仓库内置 widget_classes 目录：<repo>/pyclaego/src/personal_space/widget_classes/widgets/
# 该文件位置：<repo>/pyclaego/src/personal_space/widget_classes/registry.py
_BUILTIN_ROOT = Path(__file__).resolve().parent / "widgets"


class WidgetClassRegistry:
    """进程级单例。"""

    _instance: WidgetClassRegistry | None = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        builtin_root: Path | None = None,
    ) -> None:
        self.builtin_root: Path = Path(builtin_root or _BUILTIN_ROOT).resolve()
        self._classes: dict[str, WidgetClassSpec] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # 单例
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls, **kwargs) -> WidgetClassRegistry:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load(self, *, force: bool = False) -> None:
        """扫描 builtin 目录，加载所有 ``widget_class.json``。"""
        with self._lock:
            if self._loaded and not force:
                return
            self._classes.clear()
            if self.builtin_root.exists():
                self._scan_root(self.builtin_root, source="builtin")
            else:
                _rlog.warning("core_service", f"[WidgetClassRegistry] builtin 根目录不存在: {self.builtin_root}")
            self._loaded = True
            _rlog.info(
                "core_service",
                f"[WidgetClassRegistry] 加载完成：共 {len(self._classes)} 个 class（builtin={self.builtin_root}）",
            )

    def _scan_root(self, root: Path, *, source: str) -> None:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "widget_class.json"
            if not manifest.exists():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                spec = WidgetClassSpec.from_dict(
                    data, asset_dir=entry, source=source
                )
            except Exception as e:
                _rlog.error("core_service", f"[WidgetClassRegistry] 解析失败 {manifest}: {e}")
                continue
            # 可选 hook：Phase 11 预留扩展点
            try:
                spec.hook_class = self._load_hook_class(entry, spec.class_id)
            except Exception:
                _rlog.exception("core_service", f"[WidgetClassRegistry] 加载 {entry}/widget_class.py 失败，忽略 hook")
            existing = self._classes.get(spec.class_id)
            if existing is not None:
                _rlog.warning(
                    "core_service",
                    f"[WidgetClassRegistry] class_id={spec.class_id} 被 {source} 覆盖（原来自 {existing.source}）",
                )
            self._classes[spec.class_id] = spec

    def _load_hook_class(
        self, class_dir: Path, class_id: str
    ) -> type[WidgetHook] | None:
        """若 <class_dir>/widget_class.py 存在，从 _BUILTIN_HOOKS 返回对应 hook 类。"""
        if not (class_dir / "widget_class.py").exists():
            return None
        hook_cls = _BUILTIN_HOOKS.get(class_id)
        if hook_cls is None:
            _rlog.warning(
                "core_service",
                f"[WidgetClassRegistry] {class_id} 有 widget_class.py 但未在 _BUILTIN_HOOKS 中注册，忽略",
            )
        return hook_cls

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, class_id: str) -> WidgetClassSpec:
        self.load()
        spec = self._classes.get(class_id)
        if spec is None:
            available = sorted(self._classes.keys())
            raise KeyError(
                f"未注册的 widget_class: {class_id!r}（已注册: {available}）"
            )
        return spec

    def has(self, class_id: str) -> bool:
        self.load()
        return class_id in self._classes

    def list(self) -> builtins.list[str]:
        self.load()
        return sorted(self._classes.keys())

    def list_specs(self) -> builtins.list[WidgetClassSpec]:
        self.load()
        return [self._classes[k] for k in sorted(self._classes.keys())]

    def get_defaults(self, class_id: str) -> dict[str, Any]:
        """``WidgetClass.defaults`` 的安全副本。未注册 class 返回空 dict。"""
        self.load()
        spec = self._classes.get(class_id)
        if spec is None:
            return {}
        return dict(spec.defaults)


__all__ = ["WidgetClassRegistry"]
