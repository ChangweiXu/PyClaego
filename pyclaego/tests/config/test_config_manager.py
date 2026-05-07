"""ConfigManager 配置解析单元测试

覆盖 8 项风险修复:
  #1 循环引用检测
  #2 延迟解析写回
  #3 "0" 不应转为 False
  #4 多环境变量字符串不应触发类型转换
  #5 None 与键不存在的区分
  #6 ConcatTag 非字符串元素处理
  #7 AbsPathTag 中非字符串环境变量
  #8 _deep_copy 克隆 Tag 对象

以及 !include / !include_dir 标签的完整测试。
"""

import os
import tempfile
import textwrap
from pathlib import Path

import pytest

import pyclaego.config.manager as _config_module
from pyclaego.config.manager import (
    AbsPathTag,
    ConcatTag,
    ConfigIncludeError,
    ConfigManager,
    IncludeDirTag,
    IncludeTag,
    JoinPathTag,
)


@pytest.fixture(autouse=True)
def _reset_global_config():
    """每个测试前重置全局配置单例，避免测试间污染"""
    _config_module._global_config = None
    yield
    _config_module._global_config = None


def _make_config(yaml_text: str) -> ConfigManager:
    """从 YAML 文符串创建 ConfigManager 实例"""
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        f.write(textwrap.dedent(yaml_text))
        f.flush()
        path = f.name
    try:
        cfg = ConfigManager(config_path=path)
    finally:
        os.unlink(path)
    return cfg


# ─── Risk #1: 循环引用检测 ───────────────────────────────────────

class TestCircularReference:
    def test_direct_circular(self):
        """A 引用 B，B 引用 A → _resolve_single_ref 应抛出 ValueError"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {"a": {"x": "@{b.y}"}, "b": {"y": "@{a.x}"}}
        mgr._resolving_keys = set()
        with pytest.raises(ValueError, match="循环配置引用"):
            mgr._resolve_config_references(mgr.config)

    def test_self_reference(self):
        """自引用 → 应抛出 ValueError"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {"a": {"x": "@{a.x}"}}
        mgr._resolving_keys = set()
        with pytest.raises(ValueError, match="循环配置引用"):
            mgr._resolve_config_references(mgr.config)

    def test_three_way_circular(self):
        """A → B → C → A 三方循环"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {"a": {"v": "@{b.v}"}, "b": {"v": "@{c.v}"}, "c": {"v": "@{a.v}"}}
        mgr._resolving_keys = set()
        with pytest.raises(ValueError, match="循环配置引用"):
            mgr._resolve_config_references(mgr.config)

    def test_circular_falls_back_gracefully(self):
        """通过 ConfigManager 构造函数加载循环引用配置时，不崩溃"""
        # 不应抛出异常（_load_config 内部 catch）
        cfg = _make_config("""
            a:
              x: "@{b.y}"
            b:
              y: "@{a.x}"
        """)
        # 加载失败后配置仍可访问（包含未解析的原始值）
        assert cfg is not None


# ─── Risk #2: 延迟解析写回 ───────────────────────────────────────

class TestLazyResolveWriteback:
    def test_out_of_order_tag_resolved(self):
        """引用写在被引用者前面 → 引用应得到正确的解析值"""
        cfg = _make_config("""
            logging:
              log_root: "@{pyclaego.root_path}/logs"
            pyclaego:
              root_path: !abs_path "~/pyclaego"
        """)
        log_root = cfg.get("logging.log_root")
        assert log_root is not None
        assert "pyclaego/logs" in log_root
        assert "<" not in str(log_root)  # 不应包含 <...Tag object>

    def test_writeback_updates_config(self):
        """延迟解析后，self.config 中的值应该被更新为最终字符串"""
        cfg = _make_config("""
            logging:
              path: "@{base.dir}"
            base:
              dir: !abs_path "~/test_wb"
        """)
        # 解析完后，base.dir 应已被写回为字符串
        base_dir = cfg.get("base.dir")
        assert isinstance(base_dir, str)
        assert "test_wb" in base_dir


# ─── Risk #3: "0" 不转为 False ───────────────────────────────────

class TestZeroNotBool:
    def test_zero_default_is_int(self):
        """${VAR:0} → int(0)，不是 False"""
        cfg = _make_config("""
            test:
              port: ${__TEST_ZERO_PORT:0}
        """)
        val = cfg.get("test.port")
        assert val == 0
        assert val is not False
        assert isinstance(val, int)

    def test_one_default_is_int(self):
        """${VAR:1} → int(1)，不是 True"""
        cfg = _make_config("""
            test:
              flag: ${__TEST_ONE_FLAG:1}
        """)
        val = cfg.get("test.flag")
        assert val == 1
        assert val is not True
        assert isinstance(val, int)

    def test_true_string_is_bool(self):
        """${VAR:true} → True (bool)"""
        cfg = _make_config("""
            test:
              enabled: ${__TEST_BOOL:true}
        """)
        assert cfg.get("test.enabled") is True

    def test_false_string_is_bool(self):
        """${VAR:false} → False (bool)"""
        cfg = _make_config("""
            test:
              enabled: ${__TEST_BOOL_F:false}
        """)
        assert cfg.get("test.enabled") is False


# ─── Risk #4: 多环境变量字符串不触发类型转换 ────────────────────

class TestMultiEnvVarNoConversion:
    def test_multi_env_stays_string(self):
        """多个 ${} 在同一字符串中 → 结果保持 str"""
        cfg = _make_config("""
            test:
              url: "${__TEST_SCHEME:http}://${__TEST_MHOST:localhost}"
        """)
        val = cfg.get("test.url")
        assert isinstance(val, str)
        assert val == "http://localhost"

    def test_multi_numeric_stays_string(self):
        """${A:1}${B:2} → "12" (str)，不是 int(12)"""
        cfg = _make_config("""
            test:
              combined: "${__TEST_N1:1}${__TEST_N2:2}"
        """)
        val = cfg.get("test.combined")
        assert isinstance(val, str)
        assert val == "12"


# ─── Risk #5: None 与键不存在的区分 ──────────────────────────────

class TestNullVsMissing:
    def test_null_value_reference_returns_none(self):
        """引用值为 null 的配置项 → 返回 None，不抛异常"""
        cfg = _make_config("""
            feature:
              param: null
            other:
              ref: "@{feature.param}"
        """)
        assert cfg.get("other.ref") is None

    def test_missing_key_raises(self):
        """引用不存在的配置项 → _resolve_single_ref 抛出 ValueError"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {"test": {"ref": "@{nonexistent.key}"}}
        mgr._resolving_keys = set()
        with pytest.raises(ValueError, match="未找到"):
            mgr._resolve_config_references(mgr.config)

    def test_missing_key_falls_back(self):
        """通过构造函数加载不存在的引用 → 不崩溃"""
        # 不应抛出异常
        cfg = _make_config("""
            test:
              ref: "@{nonexistent.key}"
        """)
        assert cfg is not None


# ─── Risk #6: ConcatTag 非字符串元素 ─────────────────────────────

class TestConcatNonString:
    def test_concat_with_int_element(self):
        """!concat 中包含被类型转换为 int 的元素"""
        cfg = _make_config("""
            server:
              port: ${__TEST_CPORT:8080}
            test:
              url: !concat ["port=", "@{server.port}"]
        """)
        assert cfg.get("test.url") == "port=8080"

    def test_concat_with_bool_element(self):
        """!concat 中包含 bool 元素"""
        cfg = _make_config("""
            feature:
              enabled: ${__TEST_CENABLED:true}
            test:
              desc: !concat ["enabled=", "@{feature.enabled}"]
        """)
        assert cfg.get("test.desc") == "enabled=True"


# ─── Risk #7: AbsPathTag 中非字符串环境变量 ──────────────────────

class TestAbsPathNonStringEnv:
    def test_abs_path_with_numeric_env(self):
        """!abs_path "${VAR:0}" → 不崩溃，正常返回路径字符串"""
        cfg = _make_config("""
            test:
              path: !abs_path "${__TEST_NUM_PATH:0}"
        """)
        val = cfg.get("test.path")
        assert isinstance(val, str)
        # 应该是某个绝对路径（以 / 开头，包含 "0"）
        assert os.path.isabs(val)

    def test_abs_path_normal(self):
        """正常 !abs_path 仍然工作"""
        cfg = _make_config("""
            test:
              path: !abs_path "~/test_abs"
        """)
        val = cfg.get("test.path")
        assert isinstance(val, str)
        assert "test_abs" in val
        assert os.path.isabs(val)


# ─── Risk #8: _deep_copy 克隆 Tag 对象 ──────────────────────────

class TestDeepCopyTags:
    def test_concat_tag_copied_independently(self):
        """ConcatTag 在 _deep_copy 后应是独立对象"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {}
        mgr._resolving_keys = set()
        original = ConcatTag(["a", "b", "c"])
        copied = mgr._deep_copy(original)
        assert isinstance(copied, ConcatTag)
        assert copied is not original
        assert copied.values is not original.values
        assert copied.values == original.values

    def test_abs_path_tag_copied_independently(self):
        """AbsPathTag 在 _deep_copy 后应是独立对象"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {}
        mgr._resolving_keys = set()
        original = AbsPathTag("~/path")
        copied = mgr._deep_copy(original)
        assert isinstance(copied, AbsPathTag)
        assert copied is not original
        assert copied.path == original.path

    def test_join_path_tag_copied_independently(self):
        """JoinPathTag 在 _deep_copy 后应是独立对象"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {}
        mgr._resolving_keys = set()
        original = JoinPathTag(["a", "b"])
        copied = mgr._deep_copy(original)
        assert isinstance(copied, JoinPathTag)
        assert copied is not original
        assert copied.parts is not original.parts
        assert copied.parts == original.parts

    def test_deep_copy_dict_with_tags(self):
        """包含 Tag 的字典在 _deep_copy 后，Tag 应是独立副本"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {}
        mgr._resolving_keys = set()
        original = {"key": ConcatTag(["x", "y"]), "nested": {"path": AbsPathTag("~/foo")}}
        copied = mgr._deep_copy(original)
        assert copied["key"] is not original["key"]
        assert copied["nested"]["path"] is not original["nested"]["path"]


# ─── 综合烟雾测试 ────────────────────────────────────────────────

class TestNormalParsing:
    def test_full_config_pattern(self):
        """完整配置模式：env vars + tags + references 全部正常工作"""
        cfg = _make_config("""
            pyclaego:
              root_path: !abs_path "~/pyclaego_test"
            server:
              host: ${__TEST_HOST:127.0.0.1}
              port: ${__TEST_PORT:18765}
            client:
              server_url: !concat ["ws://", "@{server.host}", ":", "@{server.port}"]
            logging:
              log_root: !join_path ["@{pyclaego.root_path}", "logs"]
        """)
        # 验证基本值
        assert cfg.get("server.host") == "127.0.0.1"
        assert cfg.get("server.port") == 18765

        # 验证 !concat
        url = cfg.get("client.server_url")
        assert url == "ws://127.0.0.1:18765"

        # 验证 !abs_path
        root = cfg.get("pyclaego.root_path")
        assert isinstance(root, str)
        assert os.path.isabs(root)
        assert "pyclaego_test" in root

        # 验证 !join_path
        log_root = cfg.get("logging.log_root")
        assert isinstance(log_root, str)
        assert os.path.isabs(log_root)
        assert "pyclaego_test" in log_root
        assert log_root.endswith("logs")

    def test_cross_section_references(self):
        """跨 section 引用"""
        cfg = _make_config("""
            llm:
              default_provider: "kimi"
            agent:
              llm: "@{llm.default_provider}"
        """)
        assert cfg.get("agent.llm") == "kimi"


if __name__ == "__main__":
    pytest.main([__file__])


# ─── !include 标签 ───────────────────────────────────────────────

class TestIncludeTag:
    """!include "path" 单文件引入测试"""

    def _write_file(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(textwrap.dedent(content), encoding='utf-8')
        return p

    def test_include_mapping(self, tmp_path):
        """!include 引入字典类型文件，节点被完整替换"""
        sub = self._write_file(tmp_path, "llm.yaml", """\
            default_provider: kimi
            providers:
              kimi:
                model: k2
        """)
        main = self._write_file(tmp_path, "config.yaml", f"""\
            llm: !include "{sub}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("llm.default_provider") == "kimi"
        assert cfg.get("llm.providers.kimi.model") == "k2"

    def test_include_list(self, tmp_path):
        """!include 引入列表类型文件"""
        sub = self._write_file(tmp_path, "rules.yaml", """\
            - rule_a
            - rule_b
        """)
        main = self._write_file(tmp_path, "config.yaml", f"""\
            security:
              rules: !include "{sub}"
        """)
        cfg = ConfigManager(config_path=str(main))
        rules = cfg.get("security.rules")
        assert rules == ["rule_a", "rule_b"]

    def test_include_relative_path(self, tmp_path):
        """相对路径相对于包含文件所在目录解析"""
        sub_dir = tmp_path / "conf.d"
        sub_dir.mkdir()
        sub = sub_dir / "extra.yaml"
        sub.write_text("key: value_from_sub\n", encoding='utf-8')

        main = self._write_file(tmp_path, "config.yaml", """\
            extra: !include "./conf.d/extra.yaml"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("extra.key") == "value_from_sub"

    def test_include_tilde_path(self, tmp_path, monkeypatch):
        """~/path 展开为用户主目录"""
        home = tmp_path / "fakehome"
        home.mkdir()
        sub = home / "sub.yaml"
        sub.write_text("greeting: hello\n", encoding='utf-8')
        monkeypatch.setenv("HOME", str(home))

        main = self._write_file(tmp_path, "config.yaml", """\
            data: !include "~/sub.yaml"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("data.greeting") == "hello"

    def test_env_vars_in_included_file(self, tmp_path, monkeypatch):
        """被引入文件中的 ${VAR} 在整体管道中正常解析"""
        monkeypatch.setenv("TEST_INC_HOST", "myhost")
        sub = self._write_file(tmp_path, "server.yaml", """\
            host: ${TEST_INC_HOST}
            port: ${TEST_INC_PORT:9000}
        """)
        main = self._write_file(tmp_path, "config.yaml", f"""\
            server: !include "{sub}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("server.host") == "myhost"
        assert cfg.get("server.port") == 9000

    def test_config_refs_across_include_boundary(self, tmp_path):
        """被引入文件中的 @{ref} 引用主配置中的值"""
        sub = self._write_file(tmp_path, "client.yaml", """\
            url: !concat ["ws://", "@{server.host}", ":", "@{server.port}"]
        """)
        main = self._write_file(tmp_path, "config.yaml", f"""\
            server:
              host: 127.0.0.1
              port: 8765
            client: !include "{sub}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("client.url") == "ws://127.0.0.1:8765"

    def test_nested_include(self, tmp_path):
        """A includes B includes C — 嵌套引入正常工作"""
        c = self._write_file(tmp_path, "c.yaml", "value: from_c\n")
        b = self._write_file(tmp_path, "b.yaml", f'nested: !include "{c}"\n')
        a = self._write_file(tmp_path, "config.yaml", f'root: !include "{b}"\n')
        cfg = ConfigManager(config_path=str(a))
        assert cfg.get("root.nested.value") == "from_c"

    def test_include_empty_file(self, tmp_path):
        """引入空文件 → 节点值为 None，不崩溃"""
        sub = tmp_path / "empty.yaml"
        sub.write_text("", encoding='utf-8')
        main = self._write_file(tmp_path, "config.yaml", f"""\
            optional: !include "{sub}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("optional") is None

    def test_deep_copy_include_tag(self):
        """IncludeTag 在 _deep_copy 后是独立对象"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {}
        mgr._resolving_keys = set()
        original = IncludeTag("./foo.yaml")
        copied = mgr._deep_copy(original)
        assert isinstance(copied, IncludeTag)
        assert copied is not original
        assert copied.path == original.path


# ─── !include_dir 标签 ───────────────────────────────────────────

class TestIncludeDirTag:
    """!include_dir "path" 目录合并测试"""

    def _write_dir_files(self, tmp_path: Path, files: dict) -> Path:
        """在 tmp_path 下创建 sub/ 目录，写入 files {name: content}，返回目录路径"""
        d = tmp_path / "sub"
        d.mkdir()
        for name, content in files.items():
            (d / name).write_text(textwrap.dedent(content), encoding='utf-8')
        return d

    def test_include_dir_merges_sorted(self, tmp_path):
        """多文件按字典序合并，互不冲突的键均出现"""
        d = self._write_dir_files(tmp_path, {
            "01-a.yaml": "key_a: val_a\n",
            "02-b.yaml": "key_b: val_b\n",
        })
        main = tmp_path / "config.yaml"
        main.write_text(f"tools: !include_dir \"{d}\"\n", encoding='utf-8')
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("tools.key_a") == "val_a"
        assert cfg.get("tools.key_b") == "val_b"

    def test_include_dir_last_wins(self, tmp_path):
        """同名键后加载的文件覆盖先加载的（last-wins）"""
        d = self._write_dir_files(tmp_path, {
            "01-base.yaml": "timeout: 10\nhost: base_host\n",
            "02-override.yaml": "timeout: 30\n",
        })
        main = tmp_path / "config.yaml"
        main.write_text(f"server: !include_dir \"{d}\"\n", encoding='utf-8')
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("server.timeout") == 30    # overridden
        assert cfg.get("server.host") == "base_host"  # not overridden

    def test_include_dir_empty(self, tmp_path):
        """空目录 → 返回空字典，不崩溃"""
        d = tmp_path / "empty_dir"
        d.mkdir()
        main = tmp_path / "config.yaml"
        main.write_text(f"tools: !include_dir \"{d}\"\n", encoding='utf-8')
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("tools") == {}

    def test_include_dir_mixed_extensions(self, tmp_path):
        """同时支持 .yaml 和 .yml 扩展名"""
        d = tmp_path / "mixed"
        d.mkdir()
        (d / "a.yaml").write_text("from_yaml: 1\n", encoding='utf-8')
        (d / "b.yml").write_text("from_yml: 2\n", encoding='utf-8')
        main = tmp_path / "config.yaml"
        main.write_text(f"data: !include_dir \"{d}\"\n", encoding='utf-8')
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("data.from_yaml") == 1
        assert cfg.get("data.from_yml") == 2

    def test_include_dir_nested_include(self, tmp_path):
        """目录内的文件可以包含 !include 标签"""
        sub_file = tmp_path / "detail.yaml"
        sub_file.write_text("detail: ok\n", encoding='utf-8')
        d = tmp_path / "conf"
        d.mkdir()
        (d / "main.yaml").write_text(f'section: !include "{sub_file}"\n', encoding='utf-8')
        main = tmp_path / "config.yaml"
        main.write_text(f"all: !include_dir \"{d}\"\n", encoding='utf-8')
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("all.section.detail") == "ok"

    def test_include_dir_relative_path(self, tmp_path):
        """相对路径相对于包含文件所在目录"""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "item.yaml").write_text("x: 42\n", encoding='utf-8')
        main = tmp_path / "config.yaml"
        main.write_text("data: !include_dir \"./conf.d\"\n", encoding='utf-8')
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("data.x") == 42

    def test_deep_copy_include_dir_tag(self):
        """IncludeDirTag 在 _deep_copy 后是独立对象"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {}
        mgr._resolving_keys = set()
        original = IncludeDirTag("./conf.d")
        copied = mgr._deep_copy(original)
        assert isinstance(copied, IncludeDirTag)
        assert copied is not original
        assert copied.path == original.path


# ─── !include 边界和错误情况 ─────────────────────────────────────

class TestIncludeEdgeCases:
    """错误路径和边界情况测试"""

    def _write_file(self, path: Path, content: str) -> Path:
        path.write_text(textwrap.dedent(content), encoding='utf-8')
        return path

    def test_include_missing_file_raises(self, tmp_path):
        """!include 不存在的文件 → ConfigIncludeError"""
        main = self._write_file(tmp_path / "config.yaml", """\
            data: !include "./nonexistent.yaml"
        """)
        with pytest.raises(ConfigIncludeError, match="nonexistent.yaml"):
            ConfigManager(config_path=str(main))

    def test_include_dir_missing_raises(self, tmp_path):
        """!include_dir 不存在的目录 → ConfigIncludeError"""
        main = self._write_file(tmp_path / "config.yaml", """\
            data: !include_dir "./no_such_dir"
        """)
        with pytest.raises(ConfigIncludeError, match="no_such_dir"):
            ConfigManager(config_path=str(main))

    def test_include_dir_non_dir_raises(self, tmp_path):
        """!include_dir 指向普通文件 → ConfigIncludeError"""
        f = tmp_path / "afile.yaml"
        f.write_text("x: 1\n", encoding='utf-8')
        main = self._write_file(tmp_path / "config.yaml", f"""\
            data: !include_dir "{f}"
        """)
        with pytest.raises(ConfigIncludeError, match="不是目录"):
            ConfigManager(config_path=str(main))

    def test_include_dir_non_dict_file_raises(self, tmp_path):
        """!include_dir 目录中存在非字典文件 → ConfigIncludeError"""
        d = tmp_path / "bad"
        d.mkdir()
        (d / "list.yaml").write_text("- item1\n- item2\n", encoding='utf-8')
        main = self._write_file(tmp_path / "config.yaml", f"""\
            data: !include_dir "{d}"
        """)
        with pytest.raises(ConfigIncludeError, match="顶层必须是字典"):
            ConfigManager(config_path=str(main))

    def test_cyclic_include_raises(self, tmp_path):
        """A → B → A 循环引用 → ConfigIncludeError 含引用链"""
        b = tmp_path / "b.yaml"
        a = tmp_path / "config.yaml"
        # Write b.yaml to include a (cycle)
        b.write_text(f'back: !include "{a}"\n', encoding='utf-8')
        a.write_text(f'forward: !include "{b}"\n', encoding='utf-8')
        with pytest.raises(ConfigIncludeError, match="循环.*include"):
            ConfigManager(config_path=str(a))

    def test_self_include_raises(self, tmp_path):
        """文件包含自身 → ConfigIncludeError"""
        a = tmp_path / "config.yaml"
        a.write_text(f'self: !include "{a}"\n', encoding='utf-8')
        with pytest.raises(ConfigIncludeError, match="循环.*include"):
            ConfigManager(config_path=str(a))

    def test_include_at_list_item(self, tmp_path):
        """!include 用作列表元素 — 引入标量"""
        sub = tmp_path / "val.yaml"
        sub.write_text("item_val\n", encoding='utf-8')
        main = self._write_file(tmp_path / "config.yaml", f"""\
            items:
              - !include "{sub}"
              - literal
        """)
        cfg = ConfigManager(config_path=str(main))
        items = cfg.get("items")
        assert items[0] == "item_val"
        assert items[1] == "literal"

    def test_include_tag_unreachable_in_resolve_refs(self):
        """_resolve_config_references 遇到 IncludeTag 节点应 raise RuntimeError（属于 bug）"""
        mgr = ConfigManager.__new__(ConfigManager)
        mgr.config = {"key": IncludeTag("./phantom.yaml")}
        mgr._resolving_keys = set()
        with pytest.raises(RuntimeError, match="未解析的 IncludeTag"):
            mgr._resolve_config_references(mgr.config)


if __name__ == "__main__":
    pytest.main([__file__])


# ─── !include_merge 标签 ─────────────────────────────────────────────────────

class TestIncludeMergeTag:
    """测试 !include_merge: 将外部 YAML 文件的 k/v 对合并入父 dict。"""

    def _write(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding='utf-8')
        return p

    # ─ #10: 基本合并 ──────────────────────────────────────────────────────────
    def test_basic_merge(self, tmp_path):
        """!include_merge 引入的 k/v 出现在父 dict 中"""
        ext = self._write(tmp_path, "ext.yaml", """
            provider_b:
              model: kimi
        """)
        main = self._write(tmp_path, "config.yaml", f"""
            llm:
              provider_a:
                model: gpt4
              _merge: !include_merge "{ext}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("llm.provider_a.model") == "gpt4"
        assert cfg.get("llm.provider_b.model") == "kimi"

    # ─ #11: 多次合并 ──────────────────────────────────────────────────────────
    def test_multiple_merges(self, tmp_path):
        """两个 !include_merge 均被应用"""
        ext1 = self._write(tmp_path, "ext1.yaml", "kv1: v1\n")
        ext2 = self._write(tmp_path, "ext2.yaml", "kv2: v2\n")
        main = self._write(tmp_path, "config.yaml", f"""
            parent:
              _m1: !include_merge "{ext1}"
              _m2: !include_merge "{ext2}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("parent.kv1") == "v1"
        assert cfg.get("parent.kv2") == "v2"

    # ─ #12: 后者覆盖 ──────────────────────────────────────────────────────────
    def test_later_merge_overwrites(self, tmp_path):
        """后出现的 !include_merge 遵循 deep_merge last-wins 覆盖同名 key"""
        ext1 = self._write(tmp_path, "ext1.yaml", "shared: first\nexclusive1: only1\n")
        ext2 = self._write(tmp_path, "ext2.yaml", "shared: second\nexclusive2: only2\n")
        main = self._write(tmp_path, "config.yaml", f"""
            parent:
              _m1: !include_merge "{ext1}"
              _m2: !include_merge "{ext2}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("parent.shared") == "second"       # last-wins
        assert cfg.get("parent.exclusive1") == "only1"    # 保留
        assert cfg.get("parent.exclusive2") == "only2"    # 保留

    # ─ #13: 保留原有 key ───────────────────────────────────────────────────────
    def test_merge_preserves_existing_keys(self, tmp_path):
        """原 dict 中的 key 在合并后仍然存在"""
        ext = self._write(tmp_path, "ext.yaml", "new_key: new_val\n")
        main = self._write(tmp_path, "config.yaml", f"""
            parent:
              existing: original
              _m: !include_merge "{ext}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("parent.existing") == "original"
        assert cfg.get("parent.new_key") == "new_val"

    # ─ #14: 哨兵 key 不出现 ────────────────────────────────────────────────────
    def test_merge_sentinel_key_absent(self, tmp_path):
        """哨兵 key 名不应出现在最终配置中"""
        ext = self._write(tmp_path, "ext.yaml", "real_key: real_val\n")
        main = self._write(tmp_path, "config.yaml", f"""
            parent:
              _sentinel_key: !include_merge "{ext}"
        """)
        cfg = ConfigManager(config_path=str(main))
        raw_parent = cfg.get("parent")
        assert isinstance(raw_parent, dict)
        assert "_sentinel_key" not in raw_parent
        assert cfg.get("parent.real_key") == "real_val"

    # ─ #15: 非 dict 内容报错 ───────────────────────────────────────────────────
    def test_merge_non_dict_raises(self, tmp_path):
        """文件内容是列表时报 ConfigIncludeError"""
        ext = self._write(tmp_path, "list.yaml", "- item_a\n- item_b\n")
        main = self._write(tmp_path, "config.yaml", f"""
            parent:
              _m: !include_merge "{ext}"
        """)
        with pytest.raises(ConfigIncludeError, match="顶层必须是 dict"):
            ConfigManager(config_path=str(main))

    # ─ #16: 文件不存在报错 ─────────────────────────────────────────────────────
    def test_merge_missing_file_raises(self, tmp_path):
        """引用不存在的文件时报 ConfigIncludeError"""
        main = self._write(tmp_path, "config.yaml", """
            parent:
              _m: !include_merge "./nonexistent.yaml"
        """)
        with pytest.raises(ConfigIncludeError, match="不存在"):
            ConfigManager(config_path=str(main))

    # ─ #17: 循环引用报错 ───────────────────────────────────────────────────────
    def test_merge_cycle_raises(self, tmp_path):
        """循环引用: A 包含合并 B，B 包含 A → ConfigIncludeError"""
        b_yaml = tmp_path / "b.yaml"
        a_yaml = tmp_path / "a.yaml"
        b_yaml.write_text(f'_m: !include_merge "{a_yaml}"\nextra: 1\n', encoding='utf-8')
        a_yaml.write_text(f'_m: !include_merge "{b_yaml}"\nbase: 1\n', encoding='utf-8')
        main = self._write(tmp_path, "config.yaml", f"""
            parent:
              _m: !include_merge "{a_yaml}"
        """)
        with pytest.raises(ConfigIncludeError, match="循环"):
            ConfigManager(config_path=str(main))

    # ─ #18: 合并文件中的 ${VAR} 解析 ──────────────────────────────────────────
    def test_merge_env_vars_in_merged_file(self, tmp_path, monkeypatch):
        """合并文件中的 ${VAR} 在流水线中正确解析"""
        monkeypatch.setenv("TEST_INC_MERGE_HOST", "merged-host")
        ext = self._write(tmp_path, "ext.yaml", """
            merged_host: ${TEST_INC_MERGE_HOST}
            merged_port: ${TEST_INC_MERGE_PORT:9999}
        """)
        main = self._write(tmp_path, "config.yaml", f"""
            server:
              _m: !include_merge "{ext}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("server.merged_host") == "merged-host"
        assert cfg.get("server.merged_port") == 9999

    # ─ #19: 合并文件中的 @{ref} 解析 ──────────────────────────────────────────
    def test_merge_config_ref_in_merged_file(self, tmp_path):
        """合并文件中的 @{ref} 在合并后能被 _resolve_config_references 解析"""
        ext = self._write(tmp_path, "ext.yaml", """
            derived: "@{base.value}-suffix"
        """)
        main = self._write(tmp_path, "config.yaml", f"""
            base:
              value: hello
            section:
              _m: !include_merge "{ext}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("section.derived") == "hello-suffix"

    # ─ #20: 合并文件内部使用 !include ─────────────────────────────────────────
    def test_merge_nested_include_in_merged_file(self, tmp_path):
        """合并文件本身使用普通 !include — 应该递归解析"""
        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        sub = sub_dir / "deep.yaml"
        sub.write_text("deep_key: deep_val\n", encoding='utf-8')
        ext = self._write(tmp_path, "ext.yaml", f"""
            nested: !include "{sub}"
        """)
        main = self._write(tmp_path, "config.yaml", f"""
            section:
              _m: !include_merge "{ext}"
        """)
        cfg = ConfigManager(config_path=str(main))
        assert cfg.get("section.nested.deep_key") == "deep_val"
