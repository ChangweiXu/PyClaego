"""命令树节点数据模型

纯数据类，不含任何业务逻辑。
解析器、验证器、执行器各自操作这些类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass
class RedirectSpec:
    """文件重定向规格

    对应 XML 中的 <stdin file="..."/> 或 <stdout file="..." append="false"/>
    对应 JSON 中的 {"file": "...", "append": false}
    """
    file: str
    append: bool = False


@dataclass
class CmdNode:
    """单条命令节点

    对应 XML:
        <cmd name="ls">
            <arg>-lha</arg>
            <arg>~/Documents</arg>
            <stdout file="/tmp/out.txt" append="false"/>
        </cmd>

    对应 JSON:
        {"op": "cmd", "name": "ls", "args": ["-lha", "~/Documents"],
         "stdout": {"file": "/tmp/out.txt", "append": false}}
    """
    name: str
    args: list[str] = field(default_factory=list)
    stdin: RedirectSpec | None = None
    stdout: RedirectSpec | None = None
    cwd: str | None = None


@dataclass
class PipelineNode:
    """管道节点：step[0] | step[1] | ... | step[n]

    steps 中只允许 CmdNode（不允许嵌套复合节点），
    由 TreeValidator 在验证阶段强制执行。

    对应 XML:
        <pipeline>
            <cmd name="ls"><arg>-lha</arg><arg>~/Documents</arg></cmd>
            <cmd name="grep"><arg>-E</arg><arg>\.py$</arg></cmd>
        </pipeline>

    对应 JSON:
        {"op": "pipeline", "steps": [
            {"op": "cmd", "name": "ls", "args": ["-lha", "~/Documents"]},
            {"op": "cmd", "name": "grep", "args": ["-E", "\\.py$"]}
        ]}
    """
    steps: list[CmdNode]
    cwd: str | None = None


@dataclass
class SeqNode:
    """顺序执行节点：step[0]; step[1]; ...; step[n]（始终继续）

    steps 可以是任意 Node 类型。

    对应 XML:
        <seq>
            <cmd name="mkdir"><arg>-p</arg><arg>/tmp/work</arg></cmd>
            <cmd name="ls"><arg>-lha</arg></cmd>
        </seq>

    对应 JSON:
        {"op": "seq", "steps": [...]}
    """
    steps: list[Node]
    cwd: str | None = None


@dataclass
class AndNode:
    """条件执行节点：left && right（left 成功才执行 right）

    对应 XML:
        <and>
            <cmd name="test"><arg>-d</arg><arg>/tmp</arg></cmd>
            <cmd name="echo"><arg>exists</arg></cmd>
        </and>

    对应 JSON:
        {"op": "and", "left": {...}, "right": {...}}
    """
    left: Node
    right: Node
    cwd: str | None = None


@dataclass
class OrNode:
    """条件执行节点：left || right（left 失败才执行 right）

    对应 XML:
        <or>
            <cmd name="cat"><arg>/tmp/foo</arg></cmd>
            <cmd name="echo"><arg>file not found</arg></cmd>
        </or>

    对应 JSON:
        {"op": "or", "left": {...}, "right": {...}}
    """
    left: Node
    right: Node
    cwd: str | None = None


# 联合类型别名，供类型注解使用
Node = Union[CmdNode, PipelineNode, SeqNode, AndNode, OrNode]
