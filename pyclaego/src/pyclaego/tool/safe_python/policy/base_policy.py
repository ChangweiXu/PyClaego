"""ModulePolicy 抽象基类 — 每个允许模块的安全契约"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from ..exceptions import SecurityViolationError


class ModulePolicy(ABC):
    """每个注册模块的安全规格声明。

    子类通过覆盖类变量来声明约束，无需重写方法。
    如需复杂的自定义验证逻辑，可覆盖 validate_import / validate_attribute_access。

    类变量语义：
        module_name          模块名（如 "math"、"json"）
        allowed_attributes   白名单：from module import <name> 时允许的属性名
                             空集 = 不限制（允许所有公开属性）
        blocked_attributes   黑名单：始终拒绝这些属性名（优先级高于白名单）
    """

    module_name: ClassVar[str]
    allowed_attributes: ClassVar[frozenset[str]] = frozenset()
    blocked_attributes: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def validate_import(cls, names: list[str]) -> None:
        """验证 'from module import <names>' 的导入列表。

        检查顺序：
          1. dunder 属性检查
          2. blocked_attributes 黑名单检查
          3. allowed_attributes 白名单检查（若配置了白名单）

        Args:
            names: 要导入的属性名列表

        Raises:
            SecurityViolationError: 属性名不被允许
        """
        for name in names:
            if name.startswith("__") and name.endswith("__"):
                raise SecurityViolationError(
                    f"模块 '{cls.module_name}' 不允许导入 dunder 属性: '{name}'"
                )
            if name in cls.blocked_attributes:
                raise SecurityViolationError(
                    f"模块 '{cls.module_name}' 的属性 '{name}' 被列入黑名单"
                )
            if cls.allowed_attributes and name not in cls.allowed_attributes:
                raise SecurityViolationError(
                    f"模块 '{cls.module_name}' 不允许导入属性 '{name}'，"
                    f"允许的属性: {sorted(cls.allowed_attributes)}"
                )

    @classmethod
    def validate_attribute_access(cls, attr_name: str) -> None:
        """验证属性访问（如 module.attr 或 obj.attr 中 attr 的合法性）。

        Args:
            attr_name: 属性名

        Raises:
            SecurityViolationError: 属性不被允许
        """
        if attr_name.startswith("__") and attr_name.endswith("__"):
            raise SecurityViolationError(
                f"不允许访问 dunder 属性: '.{attr_name}'"
            )
        if attr_name in cls.blocked_attributes:
            raise SecurityViolationError(
                f"模块 '{cls.module_name}' 的属性 '{attr_name}' 被列入黑名单"
            )
