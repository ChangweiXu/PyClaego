"""JSON 配置加载器 — 用于 PersonalSpace / Widget 的可热编辑 JSON 配置。

PersonalSpace 模型下，全局配置仍是 YAML（``~/pyclaego/config.yaml``），
但 PS、Widget 的运行时配置存为 **JSON**，以便 Web UI 直接读写。

本模块在 JSON 上提供与 YAML ``ConfigManager`` 等价的标签能力：

字符串内嵌（与 YAML 完全一致）：
    "${ENV_VAR}"            → 环境变量
    "${ENV_VAR:default}"    → 带默认值
    "@{a.b.c}"              → 引用同一棵树内的另一项

节点级标签（用单键对象表达 YAML 自定义标签）：
    {"!concat":     ["a", "b"]}
    {"!abs_path":   "~/foo"}
    {"!join_path":  ["a", "b"]}
    {"!include":    "./other.json"}
    {"!include_dir":"./conf.d/"}

实现策略：
- ``loads`` 把 JSON 字符串解析成 Python 树。
- ``_translate_tag_objects`` 把单键 ``"!xxx"`` 对象就地替换成 YAML 侧已存在的
  ``ConcatTag`` / ``AbsPathTag`` / ``JoinPathTag`` / ``IncludeTag`` /
  ``IncludeDirTag``（重用 ``manager`` 模块里成熟的标签语义）。
- 解析阶段复用 ``ConfigManager`` 的 ``_resolve_includes`` /
  ``_replace_env_vars`` / ``_resolve_config_references``，避免把 YAML 端
  600 行的解析器在 JSON 端再写一遍。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .manager import (
    AbsPathTag,
    ConcatTag,
    ConfigIncludeError,
    ConfigManager,
    IncludeDirTag,
    IncludeMergeTag,
    IncludeTag,
    JoinPathTag,
)

_TAG_KEYS = {
    "!concat": ConcatTag,
    "!abs_path": AbsPathTag,
    "!join_path": JoinPathTag,
    "!include": IncludeTag,
    "!include_dir": IncludeDirTag,
    "!include_merge": IncludeMergeTag,
}


def _translate_tag_objects(node: Any) -> Any:
    """把节点级标签 ``{"!xxx": payload}`` 替换为对应的 Tag 数据类实例。

    递归走整棵树。普通 dict / list 原样保留；遇到 *单键* 且键名以 ``!`` 开头
    的对象，按 ``_TAG_KEYS`` 映射构造对应 Tag 实例。
    """
    if isinstance(node, dict):
        if len(node) == 1:
            (only_key,) = node.keys()
            if isinstance(only_key, str) and only_key.startswith("!"):
                if only_key not in _TAG_KEYS:
                    raise ValueError(f"未知的 JSON 标签: {only_key}")
                payload = _translate_tag_objects(node[only_key])
                return _TAG_KEYS[only_key](payload)
        return {k: _translate_tag_objects(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_translate_tag_objects(v) for v in node]
    return node


class _JsonResolver(ConfigManager):
    """轻量 ConfigManager 子类：跳过 YAML 加载逻辑，仅借用解析机制。

    我们要复用父类的 ``_resolve_includes`` / ``_replace_env_vars`` /
    ``_resolve_config_references`` / ``_process_*`` 方法。这些方法把
    解析过程中的 ``@{a.b.c}`` 引用解到 ``self.config`` 上，
    所以构造时需要把已合并好的目标树挂到 ``self.config``。
    """

    # 关闭父类那条 "搜索 ~/pyclaego/config.yaml" 的副作用
    CONFIG_PATHS: list = []  # type: ignore[assignment]

    def __init__(self) -> None:
        # 不调 super().__init__() 以避免读取磁盘上的 YAML
        self.config = {}
        self.config_file = None
        self._resolving_keys = set()


def load_json_file(path: Path) -> Any:
    """读取一个 JSON 配置文件，返回解析 + 翻译标签后的 Python 树（**未** 解析引用）。

    这是 ``PersonalSpaceConfigManager`` 的低层加载步骤；
    实际的 env / ref / tag 解析在所有层 deep-merge 完成之后再做一次。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"JSON 配置文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return _translate_tag_objects(raw)


def load_json_str(text: str) -> Any:
    """从字符串解析（用于测试/Web 表单回写）。"""
    return _translate_tag_objects(json.loads(text))


def resolve_tree(
    tree: dict[str, Any],
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """对一棵已 deep-merge 的配置树执行 include / env / ref / 标签解析。

    Args:
        tree: 已合并好的 dict（来自 ``PersonalSpaceConfigManager.resolve``）。
        base_dir: 解析 ``!include`` / ``!include_dir`` 时的基目录。
            为 ``None`` 表示禁用 include（一般 PS / Widget 配置用不到）。

    Returns:
        新的、完全解析的配置树。
    """
    resolver = _JsonResolver()
    resolver.config = tree

    if base_dir is not None:
        tree = resolver._resolve_includes(
            tree,
            base_dir=Path(base_dir).resolve(),
            include_stack=set(),
        )
        resolver.config = tree

    tree = resolver._replace_env_vars(tree)
    resolver.config = tree
    resolver._resolve_config_references(tree)
    return tree


__all__ = [
    "ConfigIncludeError",
    "load_json_file",
    "load_json_str",
    "resolve_tree",
]
