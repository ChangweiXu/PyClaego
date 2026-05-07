"""Phase 0.2 — ConfigManager v2 单元测试

覆盖：
- ``deep_merge`` 不会做 group-level overwrite，且不修改输入
- JSON 节点级标签 ``{"!concat": ...}`` 正确翻译
- ``resolve_tree`` 上的 env / ref / 标签 / include 流程
- ``PersonalSpaceConfigManager`` 的层叠解析、缓存失效、订阅通知、写回
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pyclaego.config import (
    PersonalSpaceConfigManager,
    deep_merge,
    get_ps_config,
    get_ps_widget_config,
    load_json_str,
    resolve_tree,
)
from pyclaego.config.json_loader import _translate_tag_objects
from pyclaego.config.manager import (
    AbsPathTag,
    ConcatTag,
)

# ---------------------------------------------------------------------------
# deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_recursive_merge_preserves_unrelated_keys(self):
        base = {"server": {"host": "127.0.0.1", "port": 8765}, "x": 1}
        override = {"server": {"port": 9000}, "y": 2}
        out = deep_merge(base, override)
        assert out == {
            "server": {"host": "127.0.0.1", "port": 9000},
            "x": 1,
            "y": 2,
        }

    def test_no_group_level_overwrite(self):
        """只覆盖具体 key，不会因为 'server' 里有个新值就丢掉别的字段。"""
        base = {"agent": {"name": "a", "tools": ["t1"], "ctx": {"k": "v"}}}
        ovr = {"agent": {"tools": ["t2"]}}
        out = deep_merge(base, ovr)
        # name / ctx 保留，tools 被替换
        assert out == {"agent": {"name": "a", "tools": ["t2"], "ctx": {"k": "v"}}}

    def test_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        ovr = {"a": {"b": 2}}
        out = deep_merge(base, ovr)
        out["a"]["b"] = 99
        assert base["a"]["b"] == 1
        assert ovr["a"]["b"] == 2

    def test_skip_empty_layers(self):
        assert deep_merge({}, {"a": 1}, None, {"b": 2}) == {"a": 1, "b": 2}

    def test_rejects_non_dict_layer(self):
        with pytest.raises(TypeError):
            deep_merge({"a": 1}, [1, 2])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSON 标签翻译
# ---------------------------------------------------------------------------


class TestJsonTagTranslation:
    def test_concat_node(self):
        node = load_json_str('{"url": {"!concat": ["ws://", "host", ":", "8765"]}}')
        assert isinstance(node["url"], ConcatTag)
        assert node["url"].values == ["ws://", "host", ":", "8765"]

    def test_abs_path_node(self):
        node = load_json_str('{"p": {"!abs_path": "~/foo"}}')
        assert isinstance(node["p"], AbsPathTag)

    def test_nested_in_list(self):
        node = load_json_str('{"xs": [{"!concat": ["a", "b"]}, "raw"]}')
        assert isinstance(node["xs"][0], ConcatTag)
        assert node["xs"][1] == "raw"

    def test_unknown_tag_raises(self):
        with pytest.raises(ValueError, match="未知的 JSON 标签"):
            _translate_tag_objects({"!nope": 1})

    def test_multikey_dict_is_not_a_tag(self):
        # 形如 {"!concat": [...], "other": 1} 不视为标签（只单键时才是）
        out = load_json_str('{"!concat": [1], "other": 2}')
        assert isinstance(out, dict)
        assert "!concat" in out


# ---------------------------------------------------------------------------
# resolve_tree (env / ref / tag)
# ---------------------------------------------------------------------------


class TestResolveTree:
    def test_env_var_with_default(self, monkeypatch):
        monkeypatch.delenv("MY_TEST_HOST", raising=False)
        out = resolve_tree({"host": "${MY_TEST_HOST:127.0.0.1}"})
        assert out["host"] == "127.0.0.1"

    def test_env_var_set(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_HOST", "example.com")
        out = resolve_tree({"host": "${MY_TEST_HOST}"})
        assert out["host"] == "example.com"

    def test_config_ref(self):
        out = resolve_tree({
            "server": {"host": "127.0.0.1", "port": 8765},
            "url": "@{server.host}",
        })
        assert out["url"] == "127.0.0.1"

    def test_concat_node_resolves(self):
        tree = load_json_str(
            '{"server": {"host": "127.0.0.1", "port": 8765},'
            ' "url": {"!concat": ["ws://", "@{server.host}", ":", "@{server.port}"]}}'
        )
        out = resolve_tree(tree)
        assert out["url"] == "ws://127.0.0.1:8765"

    def test_abs_path_node_resolves(self):
        tree = load_json_str('{"p": {"!abs_path": "~/foo/bar"}}')
        out = resolve_tree(tree)
        assert out["p"] == os.path.abspath(os.path.expanduser("~/foo/bar"))


# ---------------------------------------------------------------------------
# PersonalSpaceConfigManager
# ---------------------------------------------------------------------------


@pytest.fixture
def ps_root(tmp_path: Path) -> Path:
    """搭一个 personal_spaces/alice/ 模板。"""
    root = tmp_path / "personal_spaces" / "alice"
    root.mkdir(parents=True)
    (root / "personal_space.json").write_text(
        json.dumps({"id": "alice", "title": "Alice"}), encoding="utf-8"
    )
    (root / "personal_space.config.json").write_text(
        json.dumps({"agent": {"name": "Helper", "tools": ["chat"]}}),
        encoding="utf-8",
    )
    widgets = root / "widgets"
    widgets.mkdir()
    w = widgets / "w_chat_1"
    w.mkdir()
    (w / "widget.json").write_text(
        json.dumps({"widget_id": "w_chat_1", "widget_class": "chat"}),
        encoding="utf-8",
    )
    (w / "widget.config.json").write_text(
        json.dumps({"agent": {"tools": ["chat", "memory"]}}),
        encoding="utf-8",
    )
    return root


class TestPersonalSpaceConfigManager:
    def test_load_and_list(self, ps_root: Path):
        mgr = PersonalSpaceConfigManager(ps_root)
        mgr.load()
        assert mgr.ps_id == "alice"
        assert mgr.list_widget_ids() == ["w_chat_1"]
        assert mgr.get_ps_manifest()["title"] == "Alice"

    def test_resolve_widget_layered_merge(self, ps_root: Path):
        """global ← ps ← widget_class ← widget — 验证不发生 group overwrite。"""
        global_layer = {"agent": {"model": "gpt-4o"}, "logging": {"level": "INFO"}}
        mgr = PersonalSpaceConfigManager(ps_root, global_config_provider=lambda: global_layer)
        mgr.load()
        widget_class_defaults = {"agent": {"context": "soulv5"}}
        out = mgr.resolve_widget("w_chat_1", widget_class_defaults=widget_class_defaults)
        # 全部字段都在
        assert out["agent"]["model"] == "gpt-4o"      # from global
        assert out["agent"]["name"] == "Helper"        # from ps
        assert out["agent"]["context"] == "soulv5"     # from widget_class
        assert out["agent"]["tools"] == ["chat", "memory"]  # from widget (list 整体替换)
        assert out["logging"]["level"] == "INFO"

    def test_resolve_ps_only(self, ps_root: Path):
        global_layer = {"agent": {"model": "gpt-4o"}}
        mgr = PersonalSpaceConfigManager(ps_root, global_config_provider=lambda: global_layer)
        mgr.load()
        out = mgr.resolve_ps()
        assert out["agent"] == {"model": "gpt-4o", "name": "Helper", "tools": ["chat"]}

    def test_subscribe_and_reload(self, ps_root: Path):
        mgr = PersonalSpaceConfigManager(ps_root)
        mgr.load()
        events: list = []
        mgr.subscribe(lambda scope, payload: events.append(scope))

        # 写回 widget config
        new_cfg = {"agent": {"tools": ["only_one"]}}
        mgr.write_widget_config("w_chat_1", new_cfg)

        # 触发了 widget_config 通知 + 缓存失效
        assert ("widget_config", "w_chat_1") in events
        out = mgr.resolve_widget("w_chat_1")
        assert out["agent"]["tools"] == ["only_one"]
        # ps 层的 name 仍然存在
        assert out["agent"]["name"] == "Helper"

    def test_resolve_caches_widget(self, ps_root: Path):
        mgr = PersonalSpaceConfigManager(ps_root)
        mgr.load()
        a = mgr.resolve_widget("w_chat_1")
        b = mgr.resolve_widget("w_chat_1")
        assert a == b
        # 改写后缓存应该失效
        mgr.write_widget_config("w_chat_1", {"agent": {"name": "Other"}})
        c = mgr.resolve_widget("w_chat_1")
        assert c["agent"]["name"] == "Other"


# ---------------------------------------------------------------------------
# get_ps_config / get_ps_widget_config factory functions
# ---------------------------------------------------------------------------


class TestGetPsConfig:
    """Tests for the public factory ``get_ps_config``."""

    def test_returns_personal_space_config_manager(self, ps_root: Path):
        mgr = get_ps_config(ps_root)
        assert isinstance(mgr, PersonalSpaceConfigManager)

    def test_ps_root_is_set(self, ps_root: Path):
        mgr = get_ps_config(ps_root)
        assert mgr.ps_root == ps_root.resolve()

    def test_custom_provider_is_used(self, ps_root: Path):
        sentinel = {"custom": True}
        mgr = get_ps_config(ps_root, global_config_provider=lambda: sentinel)
        mgr.load()
        out = mgr.resolve_ps()
        # custom provider data must appear in the resolved output
        assert out.get("custom") is True

    def test_default_provider_wires_get_config(self, ps_root: Path, monkeypatch):
        """Without an explicit provider, get_ps_config uses get_config().config."""
        import pyclaego.config.personal_space_config as _ps_mod

        fake_config = {"injected": "from_singleton"}

        class _FakeConfigManager:
            config = fake_config

        monkeypatch.setattr(_ps_mod, "get_config", lambda: _FakeConfigManager())

        mgr = get_ps_config(ps_root)
        mgr.load()
        out = mgr.resolve_ps()
        assert out.get("injected") == "from_singleton"

    def test_returned_manager_is_functional(self, ps_root: Path):
        """get_ps_config() + load() + list_widget_ids() roundtrip."""
        mgr = get_ps_config(ps_root, global_config_provider=lambda: {})
        mgr.load()
        assert mgr.list_widget_ids() == ["w_chat_1"]
        assert mgr.get_ps_manifest()["title"] == "Alice"

    def test_each_call_returns_new_instance(self, ps_root: Path):
        """Factory must not cache — each call returns a fresh manager."""
        a = get_ps_config(ps_root)
        b = get_ps_config(ps_root)
        assert a is not b

    def test_exportable_from_config_module(self):
        """get_ps_config must be importable from the top-level config package."""
        import pyclaego.config as cfg_module
        assert hasattr(cfg_module, "get_ps_config")
        assert cfg_module.get_ps_config is get_ps_config


class TestGetPsWidgetConfig:
    """Tests for the one-shot public factory ``get_ps_widget_config``."""

    def test_returns_dict(self, ps_root: Path):
        out = get_ps_widget_config(ps_root, "w_chat_1")
        assert isinstance(out, dict)

    def test_widget_config_is_fully_resolved(self, ps_root: Path):
        """The returned dict must contain plain Python values, no Tag objects."""
        from pyclaego.config.manager import AbsPathTag, ConcatTag, JoinPathTag
        out = get_ps_widget_config(ps_root, "w_chat_1")
        def _no_tags(obj):
            if isinstance(obj, (ConcatTag, AbsPathTag, JoinPathTag)):
                return False
            if isinstance(obj, dict):
                return all(_no_tags(v) for v in obj.values())
            if isinstance(obj, list):
                return all(_no_tags(i) for i in obj)
            return True
        assert _no_tags(out), "Returned config still contains unresolved Tag objects"

    def test_layered_merge_applied(self, ps_root: Path):
        """global ← ps ← widget_class ← widget layers must all be present."""
        global_layer = {"agent": {"model": "gpt-4o"}}
        widget_class_defaults = {"agent": {"context": "soulv5"}}
        # inject global layer via monkeypatching is awkward here; use a
        # fresh ps_root with a custom global provider via direct instantiation
        # to verify the same merge logic the one-shot helper uses.
        mgr = get_ps_config(ps_root, global_config_provider=lambda: global_layer)
        mgr.load()
        out = mgr.resolve_widget("w_chat_1", widget_class_defaults)
        assert out["agent"]["model"] == "gpt-4o"
        assert out["agent"]["context"] == "soulv5"
        assert out["agent"]["tools"] == ["chat", "memory"]

    def test_returns_independent_copy(self, ps_root: Path):
        """Two calls return separate dicts — mutating one must not affect the other."""
        a = get_ps_widget_config(ps_root, "w_chat_1")
        b = get_ps_widget_config(ps_root, "w_chat_1")
        a["agent"] = "mutated"
        assert b.get("agent") != "mutated"

    def test_exportable_from_config_module(self):
        import pyclaego.config as cfg_module
        assert hasattr(cfg_module, "get_ps_widget_config")
        assert cfg_module.get_ps_widget_config is get_ps_widget_config
