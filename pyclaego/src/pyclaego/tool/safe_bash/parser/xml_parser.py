"""XML 命令树解析器

将 LLM 输出的 XML 字符串解析为 Node 树。

支持的 XML schema：

    <bash [cwd="..."]>
        <!-- 单条命令 -->
        <cmd name="COMMAND" [cwd="..."]>
            <arg>ARG1</arg>
            <arg>ARG2</arg>
            <!-- 可选重定向 -->
            <stdin  file="PATH"/>
            <stdout file="PATH" [append="true|false"]/>
        </cmd>

        <!-- 管道：step[0] | step[1] | ... -->
        <pipeline [cwd="..."]>
            <cmd name="...">...</cmd>
            <cmd name="...">...</cmd>
        </pipeline>

        <!-- 顺序执行：step[0]; step[1]; ... -->
        <seq [cwd="..."]>
            ... (任意子节点)
        </seq>

        <!-- 条件执行：left && right -->
        <and [cwd="..."]>
            <cmd name="...">...</cmd>
            <cmd name="...">...</cmd>
        </and>

        <!-- 条件执行：left || right -->
        <or [cwd="..."]>
            <cmd name="...">...</cmd>
            <cmd name="...">...</cmd>
        </or>
    </bash>

若 <bash> 只含一个直接子节点，该子节点即为树根；
若含多个直接子节点，自动包装为 SeqNode。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..exceptions import ParseError
from ..tree.nodes import (
    AndNode,
    CmdNode,
    Node,
    OrNode,
    PipelineNode,
    RedirectSpec,
    SeqNode,
)

_COMPOSITE_TAGS = {"pipeline", "seq", "and", "or"}
_ALL_TAGS = _COMPOSITE_TAGS | {"cmd"}


class XmlParser:
    """将 XML 字符串解析为命令树节点"""

    @classmethod
    def parse(cls, text: str) -> Node:
        """解析 XML 字符串，返回根 Node。

        Args:
            text: 包含 <bash>...</bash> 的 XML 字符串

        Returns:
            Node 根节点

        Raises:
            ParseError: XML 格式错误或不符合 schema
        """
        try:
            root = ET.fromstring(text.strip())
        except ET.ParseError as e:
            raise ParseError(f"XML 解析失败: {e}") from e

        if root.tag != "bash":
            raise ParseError(f"根标签必须是 <bash>，实际为 <{root.tag}>")

        root_cwd = root.get("cwd") or None

        # 收集有效子元素（忽略纯文本/注释）
        children = [child for child in root if child.tag in _ALL_TAGS]
        if not children:
            raise ParseError("<bash> 内没有可识别的命令子元素")

        nodes = [cls._parse_node(child, root_cwd) for child in children]

        if len(nodes) == 1:
            return nodes[0]
        # 多个顶层子节点 → 隐式 seq
        return SeqNode(steps=nodes, cwd=root_cwd)

    # ------------------------------------------------------------------
    # 内部解析方法
    # ------------------------------------------------------------------

    @classmethod
    def _parse_node(cls, elem: ET.Element, parent_cwd: str | None) -> Node:
        tag = elem.tag
        if tag == "cmd":
            return cls._parse_cmd(elem, parent_cwd)
        if tag == "pipeline":
            return cls._parse_pipeline(elem, parent_cwd)
        if tag == "seq":
            return cls._parse_seq(elem, parent_cwd)
        if tag == "and":
            return cls._parse_and(elem, parent_cwd)
        if tag == "or":
            return cls._parse_or(elem, parent_cwd)
        raise ParseError(f"未知标签 <{tag}>")

    @classmethod
    def _parse_cmd(cls, elem: ET.Element, parent_cwd: str | None) -> CmdNode:
        name = elem.get("name", "").strip()
        if not name:
            raise ParseError("<cmd> 缺少必需的 name 属性")

        args: list[str] = []
        stdin_spec: RedirectSpec | None = None
        stdout_spec: RedirectSpec | None = None

        for child in elem:
            if child.tag == "arg":
                args.append((child.text or "").strip())
            elif child.tag == "stdin":
                file_ = child.get("file", "").strip()
                if not file_:
                    raise ParseError("<stdin> 缺少 file 属性")
                stdin_spec = RedirectSpec(file=file_, append=False)
            elif child.tag == "stdout":
                file_ = child.get("file", "").strip()
                if not file_:
                    raise ParseError("<stdout> 缺少 file 属性")
                append_str = child.get("append", "false").strip().lower()
                stdout_spec = RedirectSpec(file=file_, append=(append_str == "true"))
            else:
                raise ParseError(f"<cmd> 内部不允许的子标签 <{child.tag}>")

        cwd = elem.get("cwd") or parent_cwd
        return CmdNode(name=name, args=args, stdin=stdin_spec, stdout=stdout_spec, cwd=cwd)

    @classmethod
    def _parse_pipeline(cls, elem: ET.Element, parent_cwd: str | None) -> PipelineNode:
        cwd = elem.get("cwd") or parent_cwd
        children = [child for child in elem if child.tag in _ALL_TAGS]
        if len(children) < 2:
            raise ParseError("<pipeline> 至少需要 2 个子节点")

        steps: list[CmdNode] = []
        for child in children:
            if child.tag != "cmd":
                raise ParseError(
                    f"<pipeline> 的直接子节点只能是 <cmd>，实际为 <{child.tag}>"
                )
            steps.append(cls._parse_cmd(child, cwd))
        return PipelineNode(steps=steps, cwd=cwd)

    @classmethod
    def _parse_seq(cls, elem: ET.Element, parent_cwd: str | None) -> SeqNode:
        cwd = elem.get("cwd") or parent_cwd
        children = [child for child in elem if child.tag in _ALL_TAGS]
        if not children:
            raise ParseError("<seq> 至少需要 1 个子节点")
        steps = [cls._parse_node(child, cwd) for child in children]
        return SeqNode(steps=steps, cwd=cwd)

    @classmethod
    def _parse_and(cls, elem: ET.Element, parent_cwd: str | None) -> AndNode:
        cwd = elem.get("cwd") or parent_cwd
        children = [child for child in elem if child.tag in _ALL_TAGS]
        if len(children) != 2:
            raise ParseError(f"<and> 需要恰好 2 个子节点，实际 {len(children)} 个")
        left = cls._parse_node(children[0], cwd)
        right = cls._parse_node(children[1], cwd)
        return AndNode(left=left, right=right, cwd=cwd)

    @classmethod
    def _parse_or(cls, elem: ET.Element, parent_cwd: str | None) -> OrNode:
        cwd = elem.get("cwd") or parent_cwd
        children = [child for child in elem if child.tag in _ALL_TAGS]
        if len(children) != 2:
            raise ParseError(f"<or> 需要恰好 2 个子节点，实际 {len(children)} 个")
        left = cls._parse_node(children[0], cwd)
        right = cls._parse_node(children[1], cwd)
        return OrNode(left=left, right=right, cwd=cwd)
