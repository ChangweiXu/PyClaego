"""safe_bash 子包

导入此包时自动触发所有内置命令的注册（通过 registry 子包）。
"""

from .exceptions import (
    ExecutionError,
    ParseError,
    SafeBashError,
    SecurityViolationError,
    StructuralViolationError,
    UnknownCommandError,
)
from .registry import REGISTRY
from .safe_bash_tool import SafeBashTool

__all__ = [
    "REGISTRY",
    "ExecutionError",
    "ParseError",
    "SafeBashError",
    "SafeBashTool",
    "SecurityViolationError",
    "StructuralViolationError",
    "UnknownCommandError",
]
