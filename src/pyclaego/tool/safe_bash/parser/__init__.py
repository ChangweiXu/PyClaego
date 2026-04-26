"""parser 子包 — 提供统一的 parse() 入口"""

from __future__ import annotations

from ..exceptions import ParseError
from ..tree.nodes import Node
from .json_parser import JsonParser
from .xml_parser import XmlParser

__all__ = ["parse", "XmlParser", "JsonParser"]


def parse(text: str, fmt: str = "auto") -> Node:
    """将命令树字符串解析为 Node 树。

    Args:
        text: XML 或 JSON 格式的命令树字符串
        fmt:  "xml"、"json" 或 "auto"（默认）
              auto 模式：首个非空白字符为 '<' → XML，'{' → JSON

    Returns:
        Node 根节点

    Raises:
        ParseError: 格式无法识别，或内容不符合 schema
    """
    text = text.strip()
    if not text:
        raise ParseError("命令树输入为空字符串")

    resolved_fmt = _resolve_format(text, fmt)

    if resolved_fmt == "xml":
        return XmlParser.parse(text)
    return JsonParser.parse(text)


def _resolve_format(text: str, fmt: str) -> str:
    if fmt == "xml":
        return "xml"
    if fmt == "json":
        return "json"
    if fmt == "auto":
        first_char = text[0]
        if first_char == "<":
            return "xml"
        if first_char == "{":
            return "json"
        raise ParseError(
            f"auto 模式无法识别格式：首字符为 {first_char!r}，"
            "期望 '<'（XML）或 '{'（JSON）"
        )
    raise ParseError(f"未知的 fmt 参数值: {fmt!r}，合法值为 'xml'、'json'、'auto'")
