"""policy 子包

导入顺序说明：
  1. policy_registry 先被导入，REGISTRY 单例在此时创建
  2. builtin_policies 随后被导入，其 @REGISTRY.register 装饰器在导入时执行注册
"""

from . import builtin_policies  # noqa: F401 — 触发所有内置策略注册
from .base_policy import ModulePolicy
from .policy_registry import ALLOWED_BUILTINS_NAMES, GLOBAL_BLACKLIST, REGISTRY, PolicyRegistry

__all__ = [
    "ALLOWED_BUILTINS_NAMES",
    "GLOBAL_BLACKLIST",
    "REGISTRY",
    "ModulePolicy",
    "PolicyRegistry",
]
