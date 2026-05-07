"""safe_bash 异常层次结构"""


class SafeBashError(Exception):
    """所有 safe_bash 异常的基类"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f"[{self.__class__.__name__}] {self.reason}"


class ParseError(SafeBashError):
    """XML 或 JSON 输入无法被解析为合法的命令树"""


class StructuralViolationError(SafeBashError):
    """命令树结构违规（嵌套过深、pipeline 中含非 CmdNode 等）"""


class UnknownCommandError(SafeBashError):
    """命令树中包含注册表中不存在的命令"""


class SecurityViolationError(SafeBashError):
    """命令或参数违反安全策略（blocked flag、路径越界等）"""


class ExecutionError(SafeBashError):
    """子进程执行阶段出现异常（区别于非零返回码）"""
