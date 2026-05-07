"""
html_to_md — HTML → Markdown 转换器模块。

为 WebFetchToolV3 提供可插拔的转换器体系。
每个转换器遵循签名: (html_raw: str, soup: BeautifulSoup) -> md_text: str

通过 converter_registry 注册和调度。
"""

from collections.abc import Callable

from bs4 import BeautifulSoup

# ── 类型定义 ──────────────────────────────────────────
ConverterFunc = Callable[[str, BeautifulSoup], str]
"""转换器签名: (html_raw, soup_parsed) -> md_text"""

# ── 转换器注册表 ──────────────────────────────────────
_converter_registry: dict[str, ConverterFunc] = {}
"""mode name -> converter function"""


def register_converter(mode: str, converter: ConverterFunc) -> None:
    """注册一个转换器。

    Args:
        mode: 模式名，如 "generic"、"arxiv"。
              与 WebFetchToolV3 参数 mode= 对应。
        converter: 转换函数，(html_raw, soup) -> md_text
    """
    _converter_registry[mode] = converter


def get_converter(mode: str) -> ConverterFunc:
    """获取已注册的转换器。

    Args:
        mode: 模式名

    Returns:
        转换器函数

    Raises:
        KeyError: 模式未注册
    """
    if mode not in _converter_registry:
        raise KeyError(
            f"未注册的转换模式: {mode!r}。"
            f"已注册: {list(_converter_registry.keys())}"
        )
    return _converter_registry[mode]


def list_converters() -> list[str]:
    """列出所有已注册的转换模式名。"""
    return list(_converter_registry.keys())


# ── 子模块 import（触发注册副作用）─────────────────────
from . import (
    arxiv_converter,  # noqa: F401 注册 "arxiv"
    generic_converter,  # noqa: F401 注册 "generic"
)

# TODO: 未来可新增:
# from . import medium_converter  # Medium 博客专用
# from . import docs_converter    # 技术文档（ReadTheDocs 等）


__all__ = [
    "ConverterFunc",
    "get_converter",
    "list_converters",
    "register_converter",
]
