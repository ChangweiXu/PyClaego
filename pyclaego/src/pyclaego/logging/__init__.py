"""日志模块 - 提供全局日志管理功能"""

# from .log_manager import get_logger, get_log_manager, LogManager
from .dynamic_logger import DynamicLogger, get_dynamic_logger
from .running_log import RunningLog, get_running_log

__all__ = [
    # "get_logger",
    # "get_log_manager",
    # "LogManager",
    "RunningLog",
    "get_running_log",
    "DynamicLogger",
    "get_dynamic_logger",
]
