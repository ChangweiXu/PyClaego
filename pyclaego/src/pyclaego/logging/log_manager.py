"""全局日志管理器 - 单例模式"""

import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional


class JSONFormatter(logging.Formatter):
    """JSON 格式化器 - 将日志输出为 JSON 格式"""
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为 JSON
        
        Args:
            record: 日志记录对象
            
        Returns:
            JSON 格式的日志字符串
        """
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # 添加异常信息（如果有）
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # 添加额外字段（如果有）
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data, ensure_ascii=False)


class LogManager:
    """全局日志管理器（单例模式）
    
    功能：
    - 统一管理所有模块的日志记录器
    - 支持控制台和文件输出
    - 支持多种日志格式（text、json）
    - 支持日志轮转
    - 每个模块独立的日志记录器
    
    使用方式：
    ```python
    from src.logging import get_logger
    
    logger = get_logger(__name__)
    logger.info("This is a log message")
    logger.error("This is an error", exc_info=True)
    ```
    """
    
    _instance: Optional["LogManager"] = None
    _initialized: bool = False
    
    def __new__(cls) -> "LogManager":
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化日志管理器（仅第一次创建时执行）"""
        if self._initialized:
            return
        
        # 日志记录器缓存
        self._loggers: dict[str, logging.Logger] = {}
        
        # 从配置文件加载配置
        self._load_config()
        
        # 创建日志目录
        self._ensure_log_directory()
        
        self._initialized = True
        
        # 使用日志系统记录初始化信息
        init_logger = self.get_logger("LogManager")
        init_logger.info(f"日志管理器已初始化 (level={self.level}, format={self.format})")
        init_logger.info(f"日志目录: {self.log_root}")
    
    def _load_config(self) -> None:
        """从配置文件加载日志配置"""
        try:
            from ..config import PYCLAEGO_DEFAULT_LOG_ROOT, get_config
            config = get_config()
            logging_config = config.get("logging", {})
            
            # 日志级别
            level_str = logging_config.get("level", "INFO").upper()
            self.level = getattr(logging, level_str, logging.INFO)
            
            # 日志格式
            self.format = logging_config.get("format", "text")  # text 或 json
            
            # 日志根目录
            log_root_str = logging_config.get("log_root", PYCLAEGO_DEFAULT_LOG_ROOT)
            self.log_root = Path(log_root_str).expanduser().resolve()
            
            # 是否启用文件日志
            self.file_enabled = logging_config.get("file_enabled", True)
            
            # 是否启用控制台日志
            self.console_enabled = logging_config.get("console_enabled", True)
            
            # 文件轮转配置
            rotation_config = logging_config.get("rotation", {})
            self.rotation_type = rotation_config.get("type", "size")  # size 或 time
            self.max_bytes = rotation_config.get("max_bytes", 10 * 1024 * 1024)  # 10MB
            self.backup_count = rotation_config.get("backup_count", 5)
            self.when = rotation_config.get("when", "midnight")  # 时间轮转：midnight, H, D, W
            
        except Exception as e:
            # 配置加载失败时使用默认配置
            print(f"[LogManager] 加载配置失败，使用默认配置: {e}")
            self.level = logging.INFO
            self.format = "text"
            from ..config import PYCLAEGO_DEFAULT_LOG_ROOT
            self.log_root = Path(PYCLAEGO_DEFAULT_LOG_ROOT).expanduser().resolve()
            self.file_enabled = True
            self.console_enabled = True
            self.rotation_type = "size"
            self.max_bytes = 10 * 1024 * 1024
            self.backup_count = 5
            self.when = "midnight"
    
    def _ensure_log_directory(self) -> None:
        """确保日志目录存在"""
        try:
            self.log_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[LogManager] 创建日志目录失败: {e}")
            # 回退到当前目录
            self.log_root = Path("./logs").resolve()
            self.log_root.mkdir(parents=True, exist_ok=True)
    
    def get_logger(self, module_name: str) -> logging.Logger:
        """获取指定模块的日志记录器
        
        Args:
            module_name: 模块名称（通常使用 __name__）
            
        Returns:
            logging.Logger: 日志记录器实例
            
        Example:
            >>> logger = get_logger(__name__)
            >>> logger.info("Hello, world!")
        """
        # 如果已经创建过，直接返回
        if module_name in self._loggers:
            return self._loggers[module_name]
        
        # 创建新的日志记录器
        logger = logging.getLogger(module_name)
        logger.setLevel(self.level)
        
        # 防止日志传播到父记录器（避免重复输出）
        logger.propagate = False
        
        # 清除已有的 handlers（避免重复添加）
        logger.handlers.clear()
        
        # 创建格式化器
        formatter = self._create_formatter()
        
        # 添加控制台 handler
        if self.console_enabled:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(self.level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # 添加文件 handler
        if self.file_enabled:
            file_handler = self._create_file_handler(module_name)
            file_handler.setLevel(self.level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        
        # 缓存日志记录器
        self._loggers[module_name] = logger
        
        return logger
    
    def _create_formatter(self) -> logging.Formatter:
        """创建日志格式化器
        
        Returns:
            logging.Formatter: 格式化器实例
        """
        if self.format == "json":
            return JSONFormatter()
        else:
            # Text 格式
            format_string = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
            date_format = "%Y-%m-%d %H:%M:%S"
            return logging.Formatter(format_string, datefmt=date_format)
    
    def _create_file_handler(self, module_name: str) -> logging.Handler:
        """创建文件 handler（支持日志轮转）
        
        Args:
            module_name: 模块名称
            
        Returns:
            logging.Handler: 文件处理器
        """
        # 文件名：使用模块名（替换特殊字符）
        safe_module_name = module_name.replace(".", "_").replace("/", "_")
        log_file = self.log_root / f"{safe_module_name}.log"
        
        # 根据轮转类型创建不同的 handler
        if self.rotation_type == "time":
            # 基于时间的轮转
            handler = TimedRotatingFileHandler(
                filename=log_file,
                when=self.when,
                backupCount=self.backup_count,
                encoding="utf-8"
            )
        else:
            # 基于大小的轮转（默认）
            handler = RotatingFileHandler(
                filename=log_file,
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8"
            )
        
        return handler
    
    def set_level(self, level: int) -> None:
        """动态设置所有日志记录器的级别
        
        Args:
            level: 日志级别（logging.DEBUG, logging.INFO, 等）
        """
        self.level = level
        
        for logger in self._loggers.values():
            logger.setLevel(level)
            for handler in logger.handlers:
                handler.setLevel(level)
        
        # 记录级别变更
        logger = self.get_logger("LogManager")
        logger.info(f"全局日志级别已更改为: {logging.getLevelName(level)}")
    
    def get_logger_names(self) -> list:
        """获取所有已创建的日志记录器名称
        
        Returns:
            list: 日志记录器名称列表
        """
        return list(self._loggers.keys())
    
    def get_stats(self) -> dict:
        """获取日志管理器统计信息
        
        Returns:
            dict: 统计信息
        """
        return {
            "total_loggers": len(self._loggers),
            "logger_names": self.get_logger_names(),
            "level": logging.getLevelName(self.level),
            "format": self.format,
            "log_root": str(self.log_root),
            "file_enabled": self.file_enabled,
            "console_enabled": self.console_enabled,
            "rotation_type": self.rotation_type
        }


# 全局单例实例
_log_manager: LogManager | None = None


def get_logger(module_name: str) -> logging.Logger:
    """获取日志记录器的便捷函数
    
    Args:
        module_name: 模块名称（通常使用 __name__）
        
    Returns:
        logging.Logger: 日志记录器实例
        
    Example:
        >>> from src.logging import get_logger
        >>> logger = get_logger(__name__)
        >>> logger.info("Application started")
        >>> logger.error("An error occurred", exc_info=True)
    """
    global _log_manager
    
    if _log_manager is None:
        _log_manager = LogManager()
    
    return _log_manager.get_logger(module_name)


def get_log_manager() -> LogManager:
    """获取日志管理器实例
    
    Returns:
        LogManager: 日志管理器单例
    """
    global _log_manager
    
    if _log_manager is None:
        _log_manager = LogManager()
    
    return _log_manager
