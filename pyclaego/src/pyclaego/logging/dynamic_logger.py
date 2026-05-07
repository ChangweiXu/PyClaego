"""动态模块日志管理器 - 按模块分文件 + 控制台级别过滤

融合 LogManager（基于 Python logging）和 RunningLog（按 name 分文件）的优点：
- 基于 Python 标准 logging 框架
- 按模块名称将日志写入不同文件（按天轮转）
- 控制台输出可按级别过滤
- warning/error/exception 自动检测并附加 traceback

文件命名规则：{log_root}/dynamic/{module_name}-{YYYY-MM-DD}.log

使用示例：
    from pyclaego.logging import get_dynamic_logger

    logger = get_dynamic_logger()
    logger.info('auth', '用户登录成功')      # 写入 auth-2026-05-01.log
    logger.error('payment', '支付失败')       # 写入 payment-2026-05-01.log
"""

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# 默认控制台输出级别
DEFAULT_CONSOLE_LEVELS = ['WARNING', 'ERROR', 'CRITICAL']

# 默认日志格式（与 LogManager 一致）
DEFAULT_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _DailyRotatingFileHandler(logging.FileHandler):
    """按天轮转的文件 Handler

    文件名格式：{base_name}-{YYYY-MM-DD}.log
    跨天时自动切换到新文件
    """

    def __init__(
        self,
        base_path: Path,
        encoding: str = "utf-8",
    ):
        """初始化

        Args:
            base_path: 日志文件基础路径（不含日期），如 /path/to/module.log
            encoding: 文件编码
        """
        self._base_path = base_path
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        current_path = self._build_path()
        super().__init__(current_path, mode='a', encoding=encoding)

    def _build_path(self) -> Path:
        """构建当前日期对应的日志文件路径"""
        stem = self._base_path.stem
        suffix = self._base_path.suffix
        return self._base_path.parent / f"{stem}-{self._current_date}{suffix}"

    def emit(self, record: logging.LogRecord) -> None:
        """发射日志记录，跨天时切换文件

        Args:
            record: 日志记录对象
        """
        current_date = datetime.now().strftime("%Y-%m-%d")
        if current_date != self._current_date:
            self.close()
            self._current_date = current_date
            self.baseFilename = str(self._build_path())
            self.stream = self._open()
        super().emit(record)


class _ConsoleLevelFilter(logging.Filter):
    """控制台级别过滤器 - 只允许指定级别通过"""

    def __init__(self, allowed_levels: list[str]):
        """初始化过滤器

        Args:
            allowed_levels: 允许输出的级别列表（如 ['WARNING', 'ERROR', 'CRITICAL']）
        """
        super().__init__()
        self._allowed_levels = {lvl.upper() for lvl in allowed_levels}

    def filter(self, record: logging.LogRecord) -> bool:
        """判断日志记录是否应输出到控制台

        Args:
            record: 日志记录对象

        Returns:
            True 表示允许输出，False 表示过滤掉
        """
        return record.levelname in self._allowed_levels


class DynamicLogger:
    """动态模块日志管理器

    所有日志写入本地文件（按模块分文件），控制台输出可按级别过滤。
    日志根目录从配置读取，实际存储路径为 {log_root}/dynamic/

    调用方式:
        logger = DynamicLogger(config)
        logger.info('auth', '用户登录成功')      # 写入 auth-2026-05-01.log
        logger.error('payment', '支付失败')       # 写入 payment-2026-05-01.log
    """

    def __init__(
        self,
        config: dict[str, Any],
        console_levels: list[str] | None = None,
        file_level: int = logging.DEBUG,
        encoding: str = "utf-8"
    ) -> None:
        """初始化动态日志管理器

        Args:
            config: 完整配置字典（从 ConfigManager.get().to_dict() 获取）
            console_levels: 需要打印到控制台的级别列表，默认 ['WARNING', 'ERROR', 'CRITICAL']
            file_level: 文件记录最低级别，默认 DEBUG
            encoding: 日志文件编码，默认 utf-8
        """
        self.config: dict[str, Any] = config
        self.console_levels: list[str] = console_levels or DEFAULT_CONSOLE_LEVELS
        self.file_level: int = file_level
        self.encoding: str = encoding

        # 日志根目录: {log_root}/dynamic/
        self.log_root: Path = self._resolve_log_root()

        # 缓存已创建的模块 logger: {module_name: logging.Logger}
        self._loggers: dict[str, logging.Logger] = {}

        # 线程锁，保护 _loggers 字典
        self._lock: threading.Lock = threading.Lock()

        # 日志格式化器
        self._formatter: logging.Formatter = self._create_formatter()

        # 共享控制台 handler（懒加载）
        self._console_handler: logging.StreamHandler | None = None
        self._console_filter: _ConsoleLevelFilter = _ConsoleLevelFilter(self.console_levels)

        # 初始化控制台 handler
        self._setup_console_handler()

    def _resolve_log_root(self) -> Path:
        """解析日志根目录

        从配置读取 logging.log_root，并追加 dynamic/ 子目录

        Returns:
            日志根目录 Path 对象
        """
        try:
            log_root_str = self.config.get('logging', {}).get('log_root', '')
            if log_root_str:
                base = Path(log_root_str).expanduser().resolve()
            else:
                # 降级到默认值
                from ..config import PYCLAEGO_DEFAULT_LOGS_ROOT
                base = Path(PYCLAEGO_DEFAULT_LOGS_ROOT).expanduser().resolve()
        except Exception as e:
            print(f"[DynamicLogger] 加载日志根目录配置失败，使用默认值: {e}")
            try:
                from ..config import PYCLAEGO_DEFAULT_LOGS_ROOT
                base = Path(PYCLAEGO_DEFAULT_LOGS_ROOT).expanduser().resolve()
            except Exception:
                base = Path("./logs").resolve()

        log_root = base / "dynamic"
        try:
            log_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[DynamicLogger] 创建日志目录失败: {e}")
            log_root = Path("./logs/dynamic").resolve()
            log_root.mkdir(parents=True, exist_ok=True)

        return log_root

    def _create_formatter(self) -> logging.Formatter:
        """创建日志格式化器

        Returns:
            logging.Formatter: 格式化器实例
        """
        return logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

    def _setup_console_handler(self) -> None:
        """配置共享控制台 Handler

        创建 StreamHandler 并添加自定义过滤器，只允许 console_levels 中的级别通过
        """
        self._console_handler = logging.StreamHandler(sys.stdout)
        self._console_handler.setLevel(self.file_level)
        self._console_handler.setFormatter(self._formatter)
        self._console_handler.addFilter(self._console_filter)

    def _get_module_logger(self, module_name: str) -> logging.Logger:
        """获取或创建模块专属 Logger

        首次调用时创建新 logger + FileHandler，后续调用复用缓存

        Args:
            module_name: 模块名称，用于日志文件名

        Returns:
            配置好的 logging.Logger 实例
        """
        # 快速路径：已缓存
        if module_name in self._loggers:
            return self._loggers[module_name]

        # 双重检查锁
        with self._lock:
            if module_name in self._loggers:
                return self._loggers[module_name]

            logger = logging.getLogger(f"pyclaego.dynamic.{module_name}")
            logger.setLevel(self.file_level)
            logger.propagate = False  # 防止传播到父 logger
            logger.handlers.clear()   # 清除已有 handlers

            # 添加文件 handler
            file_handler = self._create_file_handler(module_name)
            file_handler.setLevel(self.file_level)
            file_handler.setFormatter(self._formatter)
            logger.addHandler(file_handler)

            # 添加共享控制台 handler
            if self._console_handler:
                logger.addHandler(self._console_handler)

            self._loggers[module_name] = logger
            return logger

    def _create_file_handler(self, module_name: str) -> logging.Handler:
        """创建模块文件 Handler

        文件名格式: {module_name}-{YYYY-MM-DD}.log
        使用自定义 _DailyRotatingFileHandler 按天轮转

        Args:
            module_name: 模块名称

        Returns:
            配置好的文件 Handler
        """
        # 净化模块名称，替换文件系统不允许的字符
        safe_name = self._sanitize_module_name(module_name)
        log_file = self.log_root / f"{safe_name}.log"

        handler = _DailyRotatingFileHandler(
            base_path=log_file,
            encoding=self.encoding,
        )

        return handler

    @staticmethod
    def _sanitize_module_name(module_name: str) -> str:
        """净化模块名称，替换文件系统不允许的字符为下划线

        Args:
            module_name: 原始模块名称

        Returns:
            净化后的安全名称
        """
        invalid_chars = r'/\:*?"<>|'
        for ch in invalid_chars:
            module_name = module_name.replace(ch, "_")
        return module_name

    def _log(
        self,
        level: int,
        module_name: str,
        msg: str,
        *args: Any,
        exc_info: Any | None = None,
        **kwargs: Any
    ) -> None:
        """统一日志记录入口

        Args:
            level: 日志级别（logging.DEBUG/INFO/WARNING/ERROR/CRITICAL）
            module_name: 模块名称
            msg: 日志消息（支持 % 格式化）
            *args: 格式化参数
            exc_info: 异常信息
                - True: 使用 sys.exc_info() 获取当前异常
                - Exception 实例: 直接使用该异常
                - None: 不记录异常栈
            **kwargs: 额外参数（如 stack_info, extra 等）
        """
        logger = self._get_module_logger(module_name)
        logger.log(level, msg, *args, exc_info=exc_info, **kwargs)

    def debug(self, module_name: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录 DEBUG 级别日志

        Args:
            module_name: 模块名称
            msg: 日志消息
            *args: 格式化参数
            **kwargs: 额外参数
        """
        self._log(logging.DEBUG, module_name, msg, *args, **kwargs)

    def info(self, module_name: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录 INFO 级别日志

        Args:
            module_name: 模块名称
            msg: 日志消息
            *args: 格式化参数
            **kwargs: 额外参数
        """
        self._log(logging.INFO, module_name, msg, *args, **kwargs)

    def warning(self, module_name: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录 WARNING 级别日志

        自动检测是否有异常栈，如有则附加到日志中

        Args:
            module_name: 模块名称
            msg: 日志消息
            *args: 格式化参数
            **kwargs: 额外参数
        """
        kwargs = self._detect_and_attach_exc_info(kwargs)
        self._log(logging.WARNING, module_name, msg, *args, **kwargs)

    def error(self, module_name: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录 ERROR 级别日志

        自动检测是否有异常栈，如有则附加到日志中

        Args:
            module_name: 模块名称
            msg: 日志消息
            *args: 格式化参数
            **kwargs: 额外参数
        """
        kwargs = self._detect_and_attach_exc_info(kwargs)
        self._log(logging.ERROR, module_name, msg, *args, **kwargs)

    def exception(self, module_name: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录 EXCEPTION 级别日志

        默认附加当前异常栈（exc_info=True）

        Args:
            module_name: 模块名称
            msg: 日志消息
            *args: 格式化参数
            **kwargs: 额外参数
        """
        # exception 方法始终附加异常信息
        if 'exc_info' not in kwargs:
            kwargs['exc_info'] = True
        self._log(logging.ERROR, module_name, msg, *args, **kwargs)

    @staticmethod
    def _detect_and_attach_exc_info(kwargs: dict[str, Any]) -> dict[str, Any]:
        """检测并附加异常栈到 kwargs

        检查当前是否有活跃的异常（sys.exc_info()），如有且 kwargs 中未指定 exc_info，
        则自动设置 exc_info=True

        Args:
            kwargs: 原始关键字参数

        Returns:
            更新后的 kwargs
        """
        if 'exc_info' in kwargs:
            return kwargs

        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_type is not None and exc_value is not None:
            kwargs['exc_info'] = True

        return kwargs

    def get_log_file_path(self, module_name: str, dt: datetime | None = None) -> Path:
        """获取指定模块在指定日期（默认今天）对应的日志文件路径

        可用于外部模块查询日志文件位置，无需实际写入。

        Args:
            module_name: 模块名称
            dt: 目标日期，默认为当前时刻

        Returns:
            日志文件的完整路径（文件不一定存在）
        """
        safe_name = self._sanitize_module_name(module_name)
        date_str = (dt or datetime.now()).strftime("%Y-%m-%d")
        return self.log_root / f"{safe_name}-{date_str}.log"

    def get_stats(self) -> dict[str, Any]:
        """获取日志管理器统计信息

        Returns:
            dict: 统计信息
        """
        return {
            "total_loggers": len(self._loggers),
            "logger_names": list(self._loggers.keys()),
            "file_level": logging.getLevelName(self.file_level),
            "console_levels": self.console_levels,
            "log_root": str(self.log_root),
        }


# 模块级单例（延迟初始化）
_dynamic_logger: DynamicLogger | None = None


def get_dynamic_logger(
    config: dict[str, Any] | None = None,
    console_levels: list[str] | None = None,
    file_level: int = logging.DEBUG,
    encoding: str = "utf-8"
) -> DynamicLogger:
    """获取 DynamicLogger 单例实例

    Args:
        config: 完整配置字典（首次调用时提供，后续调用可省略）
        console_levels: 需要打印到控制台的级别列表
        file_level: 文件记录最低级别
        encoding: 日志文件编码

    Returns:
        DynamicLogger: 单例实例

    Example:
        >>> from pyclaego.logging import get_dynamic_logger
        >>> logger = get_dynamic_logger(config)
        >>> logger.info('auth', '用户登录成功')
    """
    global _dynamic_logger

    if _dynamic_logger is None:
        if config is None:
            # 尝试从配置管理器获取
            try:
                from ..config import get_config
                config = get_config().to_dict()
            except Exception as e:
                print(f"[DynamicLogger] 无法获取配置，使用空配置: {e}")
                config = {}

        _dynamic_logger = DynamicLogger(
            config=config,
            console_levels=console_levels,
            file_level=file_level,
            encoding=encoding
        )

    return _dynamic_logger
