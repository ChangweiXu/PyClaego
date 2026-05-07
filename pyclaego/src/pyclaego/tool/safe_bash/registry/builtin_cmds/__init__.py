"""builtin_cmds 子包 — 导入所有内置命令模块以触发 @REGISTRY.register 副作用"""

from . import (
    git_cmds,
    network_cmds,
    process_cmds,
    readonly_cmds,
)

__all__ = ["git_cmds", "network_cmds", "process_cmds", "readonly_cmds"]
