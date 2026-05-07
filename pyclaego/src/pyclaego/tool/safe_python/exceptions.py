"""safe_python 异常层次结构"""


class SafePythonError(Exception):
    """所有 safe_python 异常的基类"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f"[{self.__class__.__name__}] {self.reason}"


class StructuralViolationError(SafePythonError):
    """代码结构违规（嵌套过深、语句数过多、行数超限等）"""


class SecurityViolationError(SafePythonError):
    """代码违反安全策略（未注册模块、危险内置函数、dunder 访问等）"""


class ExecutionError(SafePythonError):
    """沙盒执行阶段出现异常（区别于代码本身的运行时错误）"""


class SandboxTimeoutError(SafePythonError):
    """代码执行超时"""
