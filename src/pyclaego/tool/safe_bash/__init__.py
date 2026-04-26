"""safe_bash 子包

导入此包时自动触发所有内置命令的注册（通过 registry 子包）。
"""

from .safe_bash_tool import SafeBashTool
from .exceptions import (
    SafeBashError,
    ParseError,
    StructuralViolationError,
    UnknownCommandError,
    SecurityViolationError,
    ExecutionError,
)
from .registry import REGISTRY

__all__ = [
    "SafeBashTool",
    "REGISTRY",
    "SafeBashError",
    "ParseError",
    "StructuralViolationError",
    "UnknownCommandError",
    "SecurityViolationError",
    "ExecutionError",
]
