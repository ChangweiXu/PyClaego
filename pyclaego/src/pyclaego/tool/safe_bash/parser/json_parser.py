"""JSON 命令树解析器

将 LLM 输出的 JSON 字符串解析为 Node 树。

支持的 JSON schema：

    单条命令：
        {
            "op": "cmd",
            "name": "ls",
            "args": ["-lha", "~/Documents"],
            "stdin":  {"file": "/tmp/in.txt"},              // 可选
            "stdout": {"file": "/tmp/out.txt", "append": false}, // 可选
            "cwd":    "/some/path"                          // 可选
        }

    管道（步骤只能是 cmd）：
        {
            "op": "pipeline",
            "cwd": "...",         // 可选
            "steps": [
                {"op": "cmd", "name": "ls",   "args": ["-lha", "~/Documents"]},
                {"op": "cmd", "name": "grep", "args": ["-E", "\\.py$"]}
            ]
        }

    顺序执行：
        {"op": "seq", "steps": [...任意节点...], "cwd": "..."}

    条件执行：
        {"op": "and", "left": {...}, "right": {...}, "cwd": "..."}
        {"op": "or",  "left": {...}, "right": {...}, "cwd": "..."}
"""

from __future__ import annotations

import json

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

_VALID_OPS = {"cmd", "pipeline", "seq", "and", "or"}


class JsonParser:
    """将 JSON 字符串解析为命令树节点"""

    @classmethod
    def parse(cls, text: str) -> Node:
        """解析 JSON 字符串，返回根 Node。

        Args:
            text: 包含命令树对象的 JSON 字符串

        Returns:
            Node 根节点

        Raises:
            ParseError: JSON 格式错误或不符合 schema
        """
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as e:
            raise ParseError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise ParseError("JSON 根值必须是对象（dict），而非数组或基本类型")

        return cls._parse_node(data, parent_cwd=None)

    # ------------------------------------------------------------------
    # 内部解析方法
    # ------------------------------------------------------------------

    @classmethod
    def _parse_node(cls, data: dict, parent_cwd: str | None) -> Node:
        op = data.get("op", "")
        if op not in _VALID_OPS:
            raise ParseError(
                f"未知的 op 值 '{op}'，合法值为: {sorted(_VALID_OPS)}"
            )
        if op == "cmd":
            return cls._parse_cmd(data, parent_cwd)
        if op == "pipeline":
            return cls._parse_pipeline(data, parent_cwd)
        if op == "seq":
            return cls._parse_seq(data, parent_cwd)
        if op == "and":
            return cls._parse_and(data, parent_cwd)
        # op == "or"
        return cls._parse_or(data, parent_cwd)

    @classmethod
    def _parse_cmd(cls, data: dict, parent_cwd: str | None) -> CmdNode:
        name = data.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ParseError(f"cmd 节点缺少有效的 'name' 字段: {data}")
        name = name.strip()

        raw_args = data.get("args", [])
        if not isinstance(raw_args, list):
            raise ParseError(f"cmd '{name}' 的 args 必须是数组，实际: {type(raw_args)}")
        args = [cls._coerce_str(a, f"cmd '{name}' 的 arg") for a in raw_args]

        stdin_spec = cls._parse_redirect(data.get("stdin"), f"cmd '{name}' stdin")
        stdout_spec = cls._parse_redirect(data.get("stdout"), f"cmd '{name}' stdout")

        cwd = data.get("cwd") or parent_cwd
        if cwd is not None and not isinstance(cwd, str):
            raise ParseError(f"cmd '{name}' 的 cwd 必须是字符串")

        return CmdNode(name=name, args=args, stdin=stdin_spec, stdout=stdout_spec, cwd=cwd or None)

    @classmethod
    def _parse_pipeline(cls, data: dict, parent_cwd: str | None) -> PipelineNode:
        cwd = cls._get_cwd(data, parent_cwd)
        steps_raw = data.get("steps", [])
        if not isinstance(steps_raw, list) or len(steps_raw) < 2:
            raise ParseError("pipeline 的 steps 必须是含至少 2 个元素的数组")

        steps: list[CmdNode] = []
        for i, step in enumerate(steps_raw):
            if not isinstance(step, dict):
                raise ParseError(f"pipeline.steps[{i}] 必须是对象")
            if step.get("op") != "cmd":
                raise ParseError(
                    f"pipeline.steps[{i}] 只能是 cmd，实际 op='{step.get('op')}'"
                )
            steps.append(cls._parse_cmd(step, cwd))
        return PipelineNode(steps=steps, cwd=cwd)

    @classmethod
    def _parse_seq(cls, data: dict, parent_cwd: str | None) -> SeqNode:
        cwd = cls._get_cwd(data, parent_cwd)
        steps_raw = data.get("steps", [])
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ParseError("seq 的 steps 必须是非空数组")
        steps = [
            cls._parse_node(cls._require_dict(s, f"seq.steps[{i}]"), cwd)
            for i, s in enumerate(steps_raw)
        ]
        return SeqNode(steps=steps, cwd=cwd)

    @classmethod
    def _parse_and(cls, data: dict, parent_cwd: str | None) -> AndNode:
        cwd = cls._get_cwd(data, parent_cwd)
        left = cls._parse_node(cls._require_dict(data.get("left"), "and.left"), cwd)
        right = cls._parse_node(cls._require_dict(data.get("right"), "and.right"), cwd)
        return AndNode(left=left, right=right, cwd=cwd)

    @classmethod
    def _parse_or(cls, data: dict, parent_cwd: str | None) -> OrNode:
        cwd = cls._get_cwd(data, parent_cwd)
        left = cls._parse_node(cls._require_dict(data.get("left"), "or.left"), cwd)
        right = cls._parse_node(cls._require_dict(data.get("right"), "or.right"), cwd)
        return OrNode(left=left, right=right, cwd=cwd)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_redirect(raw, label: str) -> RedirectSpec | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ParseError(f"{label} 重定向必须是对象，实际: {type(raw)}")
        file_ = raw.get("file", "")
        if not isinstance(file_, str) or not file_.strip():
            raise ParseError(f"{label} 重定向缺少有效的 'file' 字段")
        append = raw.get("append", False)
        if not isinstance(append, bool):
            raise ParseError(f"{label} 重定向的 'append' 必须是布尔值")
        return RedirectSpec(file=file_.strip(), append=append)

    @staticmethod
    def _get_cwd(data: dict, parent_cwd: str | None) -> str | None:
        cwd = data.get("cwd")
        if cwd is None:
            return parent_cwd
        if not isinstance(cwd, str):
            raise ParseError(f"cwd 必须是字符串，实际: {type(cwd)}")
        return cwd or parent_cwd

    @staticmethod
    def _coerce_str(value, label: str) -> str:
        if not isinstance(value, str):
            raise ParseError(f"{label} 必须是字符串，实际: {type(value)} ({value!r})")
        return value

    @staticmethod
    def _require_dict(value, label: str) -> dict:
        if not isinstance(value, dict):
            raise ParseError(f"{label} 必须是对象，实际: {type(value)} ({value!r})")
        return value
