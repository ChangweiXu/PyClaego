"""registry 子包

导入顺序说明：
  1. cmd_registry 先被导入，REGISTRY 单例在此时创建
  2. builtin_cmds 随后被导入，其 @REGISTRY.register 装饰器在导入时执行注册
"""

from .cmd_registry import REGISTRY, CommandRegistry
from .base_cmd import SafeCommand
from . import builtin_cmds  # noqa: F401 — 触发所有内置命令注册

__all__ = ["REGISTRY", "CommandRegistry", "SafeCommand", "builtin_cmds"]
