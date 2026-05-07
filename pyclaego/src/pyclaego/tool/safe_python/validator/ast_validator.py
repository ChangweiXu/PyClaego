"""ASTValidator — 代码结构验证器

在安全策略验证和沙盒执行之前，对代码施加全局结构约束：
- 最大嵌套深度
- 最大语句总数
- 最大源码行数
"""

from __future__ import annotations

import ast

from ..exceptions import StructuralViolationError


class ASTValidator:
    """对 AST 执行结构合法性检查（不涉及安全策略）。

    检查目的：
      - 防止深度嵌套的代码绕过分析或造成解析器栈溢出
      - 防止超长代码片段耗尽执行时间
      - 为后续策略验证提供可预期的输入规模
    """

    DEFAULT_MAX_DEPTH: int = 8
    DEFAULT_MAX_STMTS: int = 200
    DEFAULT_MAX_LINES: int = 500

    @classmethod
    def validate(
        cls,
        tree: ast.Module,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_stmts: int = DEFAULT_MAX_STMTS,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> None:
        """验证 AST 的结构约束。

        Args:
            tree:       已解析的 AST 树
            max_depth:  允许的最大嵌套深度（默认 8）
            max_stmts:  允许的最大语句总数（默认 200）
            max_lines:  允许的最大源码行数（默认 500）

        Raises:
            StructuralViolationError: 结构违规时抛出，含具体原因
        """
        # 行数检查（基于 AST 的 end_lineno 属性，Python 3.8+）
        end_line = getattr(tree, "end_lineno", None)
        if end_line is not None and end_line > max_lines:
            raise StructuralViolationError(
                f"源码行数 {end_line} 超过上限 {max_lines}"
            )

        counter = [0]  # 用列表模拟可变计数器，避免 nonlocal
        cls._walk(tree, depth=0, max_depth=max_depth, max_stmts=max_stmts, counter=counter)

    # ------------------------------------------------------------------
    # 内部遍历逻辑
    # ------------------------------------------------------------------

    @classmethod
    def _walk(
        cls,
        node: ast.AST,
        depth: int,
        max_depth: int,
        max_stmts: int,
        counter: list[int],
    ) -> None:
        if depth > max_depth:
            raise StructuralViolationError(
                f"代码嵌套深度 {depth} 超过上限 {max_depth}"
            )

        # 每遇到一个语句节点就计数
        if isinstance(node, ast.stmt):
            counter[0] += 1
            if counter[0] > max_stmts:
                raise StructuralViolationError(
                    f"代码语句总数超过上限 {max_stmts}"
                )

        for child in ast.iter_child_nodes(node):
            cls._walk(child, depth + 1, max_depth, max_stmts, counter)
