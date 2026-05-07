"""builtin_policies 子包

导入此包时自动触发所有内置策略的注册（通过 stdlib_policies 模块的
@REGISTRY.register 装饰器）。
"""

from . import stdlib_policies

__all__ = ["stdlib_policies"]
