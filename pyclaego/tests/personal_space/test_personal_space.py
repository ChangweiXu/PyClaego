"""Phase 1.1 — PersonalSpace + PersonalSpaceManager 骨架测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyclaego.personal_space import (
    DEFAULT_WIDGET_CLASS,
    DEFAULT_WIDGET_ID,
    PersonalSpace,
    PersonalSpaceManager,
    PSManifest,
    Widget,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# bootstrap_on_disk
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_creates_manifest_and_default_widget(self, tmp_path: Path):
        ps_root = tmp_path / "alice"
        PersonalSpace.bootstrap_on_disk(ps_root, "alice")

        assert (ps_root / "personal_space.json").exists()
        assert (ps_root / "personal_space.config.json").exists()

        wm_path = ps_root / "widgets" / DEFAULT_WIDGET_ID / "widget.json"
        assert wm_path.exists()
        manifest = json.loads(wm_path.read_text(encoding="utf-8"))
        assert manifest["widget_id"] == DEFAULT_WIDGET_ID
        assert manifest["widget_class"] == DEFAULT_WIDGET_CLASS

    def test_idempotent_keeps_user_edits(self, tmp_path: Path):
        ps_root = tmp_path / "alice"
        PersonalSpace.bootstrap_on_disk(ps_root, "alice")
        # 用户改了 PS title
        ps_path = ps_root / "personal_space.json"
        d = json.loads(ps_path.read_text(encoding="utf-8"))
        d["title"] = "Alice's space"
        ps_path.write_text(json.dumps(d), encoding="utf-8")
        # 再 bootstrap 一次
        PersonalSpace.bootstrap_on_disk(ps_root, "alice")
        d2 = json.loads(ps_path.read_text(encoding="utf-8"))
        assert d2["title"] == "Alice's space"


# ---------------------------------------------------------------------------
# PersonalSpace 运行时
# ---------------------------------------------------------------------------


@pytest.fixture
def ps_root(tmp_path: Path) -> Path:
    root = tmp_path / "alice"
    PersonalSpace.bootstrap_on_disk(root, "alice")
    return root


@pytest.fixture
async def loaded_ps(ps_root: Path):
    from pyclaego.config import PersonalSpaceConfigManager
    from pyclaego.personal_space import Widget, WidgetClassRegistry

    cfg = PersonalSpaceConfigManager(
        ps_root,
        global_config_provider=lambda: {
            "agent": {"type": "fake"},
            "context": {"type": "fake"},
        },
    )
    # 使用注入了 stub factory 的 Widget，避免真正构造 AgentFactory / ContextFactory
    def _stub_widget_factory(ps_id, manifest, workspace_dir, resolved_config):
        return Widget(
            ps_id=ps_id,
            manifest=manifest,
            workspace_dir=workspace_dir,
            resolved_config=resolved_config,
            agent_factory=lambda agent_cfg, sid: object(),
            context_factory=lambda sid, ws, ctx_cfg: object(),
        )

    ps = PersonalSpace(
        ps_id="alice",
        ps_root=ps_root,
        config_manager=cfg,
        widget_class_registry=WidgetClassRegistry(builtin_root=ps_root / "_no_classes"),
        widget_factory=_stub_widget_factory,
    )
    await ps.load()
    yield ps
    await ps.unload()


class TestPersonalSpace:
    async def test_loads_default_widget_manifest(self, loaded_ps: PersonalSpace):
        ids = loaded_ps.list_widget_ids()
        assert DEFAULT_WIDGET_ID in ids

    async def test_get_widget_returns_skeleton(self, loaded_ps: PersonalSpace):
        w = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        assert isinstance(w, Widget)
        assert w.widget_class == DEFAULT_WIDGET_CLASS
        assert w.belongs_to.ps_id == "alice"
        assert w.belongs_to.widget_id == DEFAULT_WIDGET_ID
        assert w.workspace_dir.exists()

    async def test_get_widget_caches_instance(self, loaded_ps: PersonalSpace):
        w1 = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        w2 = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        assert w1 is w2

    async def test_get_widget_missing_raises(self, loaded_ps: PersonalSpace):
        with pytest.raises(KeyError):
            await loaded_ps.get_widget("nonexistent")

    async def test_connection_refcount(self, loaded_ps: PersonalSpace):
        assert loaded_ps.is_idle()
        loaded_ps.open_connection("c1")
        loaded_ps.open_connection("c2")
        assert loaded_ps.active_connection_count == 2
        assert not loaded_ps.is_idle()
        loaded_ps.close_connection("c1")
        loaded_ps.close_connection("c2")
        assert loaded_ps.is_idle()

    async def test_in_flight_blocks_idle(self, loaded_ps: PersonalSpace):
        loaded_ps.inc_in_flight()
        assert not loaded_ps.is_idle()
        loaded_ps.dec_in_flight()
        assert loaded_ps.is_idle()

    async def test_get_manifest(self, loaded_ps: PersonalSpace):
        m = loaded_ps.get_manifest()
        assert isinstance(m, PSManifest)
        assert m.ps_id == "alice"
        assert DEFAULT_WIDGET_ID in m.widget_order

    async def test_widget_config_change_invalidates_cache(self, loaded_ps: PersonalSpace, ps_root: Path):
        w1 = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        # 通过 ConfigManager 写回，触发订阅器清缓存
        loaded_ps.config.write_widget_config(DEFAULT_WIDGET_ID, {"agent": {"x": 1}})
        w2 = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        assert w1 is not w2  # 重新构建
        assert w2.resolved_config.get("agent", {}).get("x") == 1

    async def test_ps_config_change_invalidates_all_widgets(self, loaded_ps: PersonalSpace, ps_root: Path):
        """回归：编辑 personal_space.config.json 必须让所有已加载 widget 重建。"""
        w1 = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        loaded_ps.config.write_ps_config({"agent": {"type": "fake", "llm": "override_llm"}})
        w2 = await loaded_ps.get_widget(DEFAULT_WIDGET_ID)
        assert w1 is not w2
        assert w2.resolved_config.get("agent", {}).get("llm") == "override_llm"


# ---------------------------------------------------------------------------
# PersonalSpaceManager
# ---------------------------------------------------------------------------


@pytest.fixture
def psm_root(tmp_path: Path) -> Path:
    root = tmp_path / "personal_spaces"
    root.mkdir()
    return root


@pytest.fixture
async def psm(psm_root: Path):
    PersonalSpaceManager.reset_instance()
    mgr = PersonalSpaceManager(root_path=psm_root, max_active=3)
    yield mgr
    await mgr.shutdown()
    PersonalSpaceManager.reset_instance()


class TestPersonalSpaceManager:
    async def test_get_creates_ps_and_files(self, psm: PersonalSpaceManager, psm_root: Path):
        ps = await psm.get("alice")
        assert ps.ps_id == "alice"
        assert (psm_root / "alice" / "personal_space.json").exists()
        assert DEFAULT_WIDGET_ID in ps.list_widget_ids()

    async def test_get_returns_same_instance(self, psm: PersonalSpaceManager):
        a = await psm.get("alice")
        b = await psm.get("alice")
        assert a is b

    async def test_invalid_ps_id_raises(self, psm: PersonalSpaceManager):
        for bad in ("../etc", ".hidden", "", "a/b", "a b"):
            with pytest.raises(ValueError):
                await psm.get(bad)

    async def test_lru_evicts_idle(self, psm: PersonalSpace, psm_root: Path):
        # max_active=3
        await psm.get("a")
        await psm.get("b")
        await psm.get("c")
        assert set(psm.list_loaded_ps_ids()) == {"a", "b", "c"}
        await psm.get("d")
        loaded = set(psm.list_loaded_ps_ids())
        assert "d" in loaded
        assert len(loaded) == 3  # 一个被卸载

    async def test_lru_keeps_active_connections(self, psm: PersonalSpaceManager):
        a = await psm.get("a")
        a.open_connection("conn_a")  # a 不再 idle
        await psm.get("b")
        await psm.get("c")
        await psm.get("d")  # 触发卸载
        assert "a" in psm.list_loaded_ps_ids()  # a 受保护

    async def test_open_close_connection(self, psm: PersonalSpaceManager):
        ps = await psm.open_connection("c1", "alice")
        assert ps.active_connection_count == 1
        await psm.close_connection("c1", "alice")
        assert ps.active_connection_count == 0

    async def test_unload_idempotent(self, psm: PersonalSpaceManager):
        await psm.get("alice")
        assert await psm.unload("alice") is True
        assert await psm.unload("alice") is False

    async def test_list_disk_ps_ids(self, psm: PersonalSpaceManager, psm_root: Path):
        await psm.get("alice")
        await psm.get("bob")
        # 在磁盘上多放一个未加载的目录
        (psm_root / "carol").mkdir()
        ids = psm.list_disk_ps_ids()
        assert ids == ["alice", "bob", "carol"]
