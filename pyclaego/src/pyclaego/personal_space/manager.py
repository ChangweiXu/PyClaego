"""``PersonalSpaceManager`` — 进程级 PS 注册表 + LRU 卸载。

职责：
- 单例
- ``async get(ps_id) -> PersonalSpace``：首次访问时引导磁盘 + 构造运行时
- LRU：内存中至多保留 ``max_active_personal_spaces`` 个 PS
  - 超额时挑选 idle 且 ``last_activity_ts`` 最旧的 PS 卸载
- ``async unload(ps_id)``：幂等
- 提供 ``open_connection(conn_id, ps_id)`` / ``close_connection(conn_id, ps_id)``
  代理给 PS（保证 PS 已加载）

不依赖 CoreScheduler / 网络协议；CoreScheduler 在 Phase 2.1 中通过本类
访问 PS。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import PYCLAEGO_DEFAULT_ROOT, get_config, get_ps_config
from ..logging import get_running_log
from .personal_space import KIND_GENERIC, PersonalSpace
from .widget_classes import WidgetClassRegistry

_rlog = get_running_log()


# ps_id 校验：避免路径穿越 / 奇怪字符。允许 [A-Za-z0-9_.-]，不能以 . 或 _ 开头，
# 不能以 _ 结尾，不能包含连续的 __ 。
_PS_ID_PATTERN = re.compile(r"^(?!_)(?!.*__)[A-Za-z0-9._-]+(?<!_)$")
DEFAULT_MAX_ACTIVE = 64


def _is_valid_ps_id(ps_id: str) -> bool:
    return bool(ps_id) and bool(_PS_ID_PATTERN.match(ps_id))


class PersonalSpaceManager:
    """单例。线程不安全，按 asyncio 单循环约束使用。"""

    _instance: PersonalSpaceManager | None = None
    _instance_lock = asyncio.Lock()

    def __init__(
        self,
        root_path: Path | None = None,
        max_active: int | None = None,
        global_config_provider: Callable[[], dict[str, Any]] | None = None,
        *,
        widget_class_registry: WidgetClassRegistry | None = None,
        widget_factory: Any | None = None,
    ) -> None:
        # 解析 root_path / max_active：优先使用显式参数，否则从全局 config 读
        cfg = get_config()
        ps_section = cfg.get("personal_space", {}) or {}

        if root_path is not None:
            self.root_path: Path = Path(root_path).expanduser().resolve()
        else:
            raw = (
                ps_section.get("root_path")
                or os.path.join(PYCLAEGO_DEFAULT_ROOT, "personal_spaces")
            )
            self.root_path = Path(raw).expanduser().resolve()

        if max_active is not None:
            self.max_active: int = int(max_active)
        else:
            self.max_active = int(ps_section.get("max_active", DEFAULT_MAX_ACTIVE))

        self._global_provider = global_config_provider or (lambda: get_config().config)
        self._widget_class_registry = widget_class_registry
        self._widget_factory = widget_factory

        self._spaces: dict[str, PersonalSpace] = {}
        self._lock = asyncio.Lock()  # 守护 _spaces / LRU

        self.root_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 单例
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls, **kwargs) -> PersonalSpaceManager:
        """返回进程级单例。首次调用时使用 ``kwargs`` 构造。"""
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """**仅供测试**：清掉单例。生产代码不要调用。"""
        cls._instance = None

    # ------------------------------------------------------------------
    # 主 API
    # ------------------------------------------------------------------

    async def get(self, ps_id: str, *, init_kind: str | None = None) -> PersonalSpace:
        """获取（或懒加载）指定 PS 实例。

        ``init_kind`` 仅在磁盘上还没有该 PS 时才有效；
        已存在的 PS 以磁盘上的 kind 为准。
        """
        if not _is_valid_ps_id(ps_id):
            raise ValueError(f"非法的 ps_id: {ps_id!r}")

        async with self._lock:
            existing = self._spaces.get(ps_id)
            if existing is not None:
                existing._touch()
                return existing

            # 触发 LRU 评估
            await self._evict_if_needed_locked(extra=1)

            ps_root = self.root_path / ps_id
            if not ps_root.exists():
                _rlog.info("core_service", f"[PSManager] 引导新 PS 目录: {ps_root}")
                PersonalSpace.bootstrap_on_disk(
                    ps_root, ps_id, kind=init_kind or KIND_GENERIC
                )

            ps_cfg = get_ps_config(
                ps_root,
                global_config_provider=self._global_provider,
            )
            ps = PersonalSpace(
                ps_id=ps_id,
                ps_root=ps_root,
                config_manager=ps_cfg,
                widget_class_registry=self._widget_class_registry,
                widget_factory=self._widget_factory,
            )
            await ps.load()
            self._spaces[ps_id] = ps
            return ps

    async def unload(self, ps_id: str) -> bool:
        """主动卸载 PS（用于显式清理 / 测试）。返回是否实际卸载了。"""
        async with self._lock:
            ps = self._spaces.pop(ps_id, None)
        if ps is None:
            return False
        try:
            await ps.unload()
        except Exception:
            _rlog.exception("core_service", f"[PSManager] PS {ps_id} 卸载时报错")
        return True

    async def shutdown(self) -> None:
        """卸载所有 PS（进程退出钩子）。"""
        async with self._lock:
            ids = list(self._spaces.keys())
        for ps_id in ids:
            await self.unload(ps_id)
        # Shut down any shared NoteVaults still held by note widgets
        try:
            from ..note_system import NoteSystemManager
            await NoteSystemManager.instance().shutdown_all()
        except Exception:
            _rlog.exception("core_service", "[PSManager] NoteSystemManager shutdown error")

    # ------------------------------------------------------------------
    # 连接计数（外部入口）
    # ------------------------------------------------------------------

    async def open_connection(self, conn_id: str, ps_id: str, *, init_kind: str | None = None) -> PersonalSpace:
        ps = await self.get(ps_id, init_kind=init_kind)
        ps.open_connection(conn_id)
        return ps

    async def close_connection(self, conn_id: str, ps_id: str) -> None:
        async with self._lock:
            ps = self._spaces.get(ps_id)
        if ps is not None:
            ps.close_connection(conn_id)

    # ------------------------------------------------------------------
    # 自省
    # ------------------------------------------------------------------

    def list_loaded_ps_ids(self) -> list[str]:
        return list(self._spaces.keys())

    def list_disk_ps_ids(self, *, exclude_kinds: set[str] | None = None) -> list[str]:
        if not self.root_path.exists():
            return []
        ids = sorted(
            entry.name
            for entry in self.root_path.iterdir()
            if entry.is_dir() and _is_valid_ps_id(entry.name)
        )
        if not exclude_kinds:
            return ids
        filtered = []
        for ps_id in ids:
            manifest_path = self.root_path / ps_id / "personal_space.json"
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                kind = raw.get("kind", "generic")
            except Exception:
                kind = "generic"
            if kind not in exclude_kinds:
                filtered.append(ps_id)
        return filtered

    def is_loaded(self, ps_id: str) -> bool:
        return ps_id in self._spaces

    # ------------------------------------------------------------------
    # 内部 — LRU
    # ------------------------------------------------------------------

    async def _evict_if_needed_locked(self, extra: int = 0) -> None:
        """在 ``self._lock`` 已持有的情况下，按 LRU 卸载冗余 PS。"""
        target = max(0, self.max_active - extra)
        if len(self._spaces) <= target:
            return

        # 候选排序：先选 idle 的，按 last_activity_ts 升序
        candidates = sorted(
            (ps for ps in self._spaces.values() if ps.is_idle()),
            key=lambda p: p.last_activity_ts,
        )
        for ps in candidates:
            if len(self._spaces) <= target:
                return
            self._spaces.pop(ps.ps_id, None)
            try:
                await ps.unload()
            except Exception:
                _rlog.exception("core_service", f"[PSManager] LRU 卸载 {ps.ps_id} 失败")

        if len(self._spaces) > target:
            _rlog.warning(
                "core_service",
                f"[PSManager] LRU 卸载后仍超额：loaded={len(self._spaces)} target={target}（剩余的都非 idle）",
            )


__all__ = ["PersonalSpaceManager"]
