"""命令树结构验证器

在注册表验证和执行之前，对树的结构施加全局约束：
- 最大嵌套深度
- 最大命令总数
- pipeline 的步骤只能是 CmdNode
- and/or 的子节点不能是 PipelineNode（避免语义歧义）
"""

from __future__ import annotations

from ..exceptions import StructuralViolationError
from .nodes import AndNode, CmdNode, Node, OrNode, PipelineNode, SeqNode


class TreeValidator:
    """对 Node 树执行结构合法性检查"""

    DEFAULT_MAX_DEPTH: int = 5
    DEFAULT_MAX_CMDS: int = 20

    @classmethod
    def validate(
        cls,
        node: Node,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_cmds: int = DEFAULT_MAX_CMDS,
    ) -> None:
        """验证整棵命令树的结构。

        Args:
            node:      树的根节点
            max_depth: 允许的最大嵌套深度（默认 5）
            max_cmds:  树中允许的最大命令总数（默认 20）

        Raises:
            StructuralViolationError: 结构违规时抛出，含具体原因
        """
        counter = [0]  # 用列表模拟可变计数器，避免 nonlocal
        cls._walk(node, depth=1, max_depth=max_depth, max_cmds=max_cmds, counter=counter)

    # ------------------------------------------------------------------
    # 内部遍历逻辑
    # ------------------------------------------------------------------

    @classmethod
    def _walk(
        cls,
        node: Node,
        depth: int,
        max_depth: int,
        max_cmds: int,
        counter: list[int],
    ) -> None:
        if depth > max_depth:
            raise StructuralViolationError(
                f"命令树嵌套深度 {depth} 超过上限 {max_depth}"
            )

        if isinstance(node, CmdNode):
            counter[0] += 1
            if counter[0] > max_cmds:
                raise StructuralViolationError(
                    f"命令树命令总数超过上限 {max_cmds}"
                )
            return  # 叶节点，无子节点

        if isinstance(node, PipelineNode):
            for step in node.steps:
                # pipeline 步骤只能是 CmdNode（在解析器已保证，此处为双重检查）
                if not isinstance(step, CmdNode):
                    raise StructuralViolationError(
                        f"pipeline 的步骤只能是 CmdNode，实际: {type(step).__name__}"
                    )
                cls._walk(step, depth + 1, max_depth, max_cmds, counter)
            return

        if isinstance(node, SeqNode):
            for step in node.steps:
                cls._walk(step, depth + 1, max_depth, max_cmds, counter)
            return

        if isinstance(node, (AndNode, OrNode)):
            node_type = type(node).__name__
            # and/or 的子节点不允许是 PipelineNode（语义不明确）
            if isinstance(node.left, PipelineNode):
                raise StructuralViolationError(
                    f"{node_type}.left 不允许是 PipelineNode"
                )
            if isinstance(node.right, PipelineNode):
                raise StructuralViolationError(
                    f"{node_type}.right 不允许是 PipelineNode"
                )
            cls._walk(node.left, depth + 1, max_depth, max_cmds, counter)
            cls._walk(node.right, depth + 1, max_depth, max_cmds, counter)
            return

        raise StructuralViolationError(f"未知节点类型: {type(node).__name__}")
