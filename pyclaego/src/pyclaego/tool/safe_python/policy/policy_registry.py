"""PolicyRegistry — 模块策略注册表，提供 AST 级安全验证"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from ..exceptions import SecurityViolationError

if TYPE_CHECKING:
    from .base_policy import ModulePolicy

# -----------------------------------------------------------------------
# 全局安全常量
# -----------------------------------------------------------------------

# 全局黑名单：这些名称在代码中出现即拒绝（无论作为调用还是引用）
GLOBAL_BLACKLIST: frozenset[str] = frozenset({
    # 代码执行
    "eval", "exec", "compile",
    # 导入钩子（由受限导入器替代）
    "__import__",
    # 内省（可绕过沙盒）
    "globals", "locals", "vars",
    # I/O（文件 I/O 通过策略授权的模块提供）
    "open", "input",
    # 进程控制
    "exit", "quit",
    # 调试入口
    "breakpoint",
    # 直接访问内置命名空间
    "__builtins__",
    # 底层对象操作
    "__loader__", "__spec__",
})

# 沙盒中允许使用的内置函数/类型/常量白名单
ALLOWED_BUILTINS_NAMES: frozenset[str] = frozenset({
    # 类型构造
    "bool", "int", "float", "complex", "str", "bytes", "bytearray",
    "list", "tuple", "set", "frozenset", "dict",
    # 迭代与函数式
    "len", "range", "print",
    "abs", "round", "divmod", "pow",
    "min", "max", "sum",
    "sorted", "reversed", "enumerate", "zip", "map", "filter",
    "all", "any", "iter", "next",
    # 字符串/数值格式化
    "repr", "format", "chr", "ord", "hex", "oct", "bin",
    # 类型检查（受限）
    "isinstance", "issubclass", "callable", "hasattr", "type",
    # 类定义内部机制（class 语句所需）
    "__build_class__",
    "object", "property", "staticmethod", "classmethod", "super",
    # 内置常量
    "NotImplemented", "Ellipsis",
    # 内置异常
    "Exception", "BaseException",
    "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
    "RuntimeError", "StopIteration", "GeneratorExit",
    "ArithmeticError", "ZeroDivisionError", "OverflowError",
    "ImportError", "ModuleNotFoundError",
    "OSError", "IOError",
    "NameError", "UnboundLocalError",
    "LookupError", "AssertionError", "NotImplementedError",
    "RecursionError", "MemoryError", "TimeoutError",
    "StopAsyncIteration", "BufferError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError",
    "Warning", "UserWarning", "DeprecationWarning", "RuntimeWarning",
    # 内置函数（不在黑名单的其余安全函数）
    "slice", "id", "hash",
})


# -----------------------------------------------------------------------
# AST 安全访问器
# -----------------------------------------------------------------------

class _SecurityVisitor(ast.NodeVisitor):
    """遍历 AST，执行所有安全策略检查。

    成功遍历后，approved_modules 包含代码中所有经过验证的顶层模块名，
    供执行器构建受限导入器。
    """

    def __init__(self, registry: PolicyRegistry) -> None:
        self._registry = registry
        # 收集通过验证的顶层模块名
        self.approved_modules: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        """处理 'import module' / 'import module as alias'"""
        for alias in node.names:
            top_module = alias.name.split(".")[0]
            # 查找策略（未注册则抛出 SecurityViolationError）
            self._registry.get(top_module)
            self.approved_modules.add(top_module)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """处理 'from module import name' / 'from module import name as alias'"""
        # 允许 __future__ 导入（仅影响编译器，无运行时风险）
        if node.module == "__future__":
            self.generic_visit(node)
            return

        if node.level != 0:
            raise SecurityViolationError("不允许相对导入（from . import ...）")
        if node.module is None:
            raise SecurityViolationError("导入语句缺少模块名")

        top_module = node.module.split(".")[0]
        policy = self._registry.get(top_module)

        # 拒绝通配符导入
        names = [a.name for a in node.names]
        if "*" in names:
            raise SecurityViolationError(
                f"不允许通配符导入 'from {node.module} import *'"
            )

        # 验证每个被导入的名称
        policy.validate_import(names)
        self.approved_modules.add(top_module)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """拦截对黑名单函数的直接调用"""
        if isinstance(node.func, ast.Name):
            if node.func.id in GLOBAL_BLACKLIST:
                raise SecurityViolationError(
                    f"调用被禁止的函数: '{node.func.id}'"
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """拦截所有 dunder 属性访问（如 obj.__class__、obj.__globals__）"""
        if (
            isinstance(node.attr, str)
            and node.attr.startswith("__")
            and node.attr.endswith("__")
        ):
            raise SecurityViolationError(
                f"不允许访问 dunder 属性: '.{node.attr}'"
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """拦截黑名单名称及 dunder 名称（无论读取、写入还是删除）"""
        # 无论操作类型（Load / Store / Del），dunder 名称一律拒绝。
        # 这同时防止读取（__builtins__["eval"]）和写入（__builtins__ = evil）。
        if node.id.startswith("__") and node.id.endswith("__"):
            raise SecurityViolationError(
                f"不允许访问或修改 dunder 名称: '{node.id}'"
            )
        if isinstance(node.ctx, ast.Load) and node.id in GLOBAL_BLACKLIST:
            raise SecurityViolationError(
                f"不允许使用被禁止的名称: '{node.id}'"
            )
        self.generic_visit(node)


# -----------------------------------------------------------------------
# 注册表
# -----------------------------------------------------------------------

class PolicyRegistry:
    """模块策略注册表单例。

    使用方式：
        @REGISTRY.register
        class MathPolicy(ModulePolicy):
            module_name = "math"
            ...
    """

    def __init__(self) -> None:
        self._policies: dict[str, type[ModulePolicy]] = {}

    def register(self, policy_class: type[ModulePolicy]) -> type[ModulePolicy]:
        """将模块策略注册到注册表（可用作装饰器）。

        Args:
            policy_class: ModulePolicy 子类

        Returns:
            原始 policy_class（装饰器语义）
        """
        module_name = getattr(policy_class, "module_name", None)
        if not module_name:
            raise ValueError(f"{policy_class.__name__} 未定义 'module_name' 类变量")
        self._policies[module_name] = policy_class
        return policy_class

    def get(self, module_name: str) -> type[ModulePolicy]:
        """按模块名查找策略类。

        Args:
            module_name: 顶层模块名（如 "math"）

        Returns:
            对应的 ModulePolicy 子类

        Raises:
            SecurityViolationError: 模块未注册
        """
        policy = self._policies.get(module_name)
        if policy is None:
            raise SecurityViolationError(
                f"模块 '{module_name}' 未在安全策略注册表中注册，"
                f"已注册的模块: {sorted(self._policies.keys())}"
            )
        return policy

    def validate_ast(self, tree: ast.Module) -> set[str]:
        """遍历 AST，执行所有安全策略检查。

        Args:
            tree: 已解析的 AST 树

        Returns:
            通过验证的顶层模块名集合（供执行器构建受限导入器）

        Raises:
            SecurityViolationError: 任何安全策略不通过时抛出
        """
        visitor = _SecurityVisitor(self)
        visitor.visit(tree)
        return visitor.approved_modules

    def list_modules(self) -> list[str]:
        """返回所有已注册模块名的有序列表。"""
        return sorted(self._policies.keys())


# 模块单例
REGISTRY = PolicyRegistry()
