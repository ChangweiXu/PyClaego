"""builtin_cmds 子包 — 导入所有内置命令模块以触发 @REGISTRY.register 副作用"""

from . import readonly_cmds  # noqa: F401
from . import git_cmds       # noqa: F401
from . import network_cmds   # noqa: F401
from . import process_cmds   # noqa: F401

__all__ = ["readonly_cmds", "git_cmds", "network_cmds", "process_cmds"]
