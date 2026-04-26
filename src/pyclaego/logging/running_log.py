"""运行日志模块 - 按 name + 日期分文件记录业务流水日志

与 LogManager 不同，RunningLog 不依赖 Python logging 框架，
直接以文本行追加写文件，适合业务运行轨迹、LLM 调用记录等场景。

文件命名规则：{log_root}/running/{name}-YYYYMMDD-run.log

使用示例：
    from pyclaego.logging import get_running_log

    rlog = get_running_log()
    rlog.info("session_abc", "Session 启动")
    rlog.warning("session_abc", "LLM 响应超时")
    rlog.error("session_abc", "工具调用失败: bash_executor")
"""

import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class RunningLog:
    """运行日志管理器（单例模式）

    功能：
    - 日志根目录从 config.yaml 的 logging.log_root 读取
    - 每次调用 log(name, message) 时，根据 name 和当前日期选择对应文件追加写入
    - 日志文件命名：{log_root}/running/{name}-YYYYMMDD-run.log
    - 跨天自动切换到新日期文件，无需手动干预
    - 线程安全：写入操作由单一全局锁保护

    配置项（config.yaml 中的 logging.running_log）：
        subdir:      子目录名，默认 "running"
        format:      行格式模板，默认 "[{time}] [{level}] {message}"
        time_format: 时间格式，默认 "%Y-%m-%d %H:%M:%S"
        encoding:    文件编码，默认 "utf-8"
    """

    _instance: Optional["RunningLog"] = None
    _initialized: bool = False

    def __new__(cls) -> "RunningLog":
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化运行日志管理器（仅第一次创建时执行）"""
        if self._initialized:
            return

        self._lock = threading.Lock()
        self._load_config()
        RunningLog._initialized = True

    def _load_config(self) -> None:
        """从 config 读取日志配置

        读取 logging.log_root 作为日志根目录，
        读取 logging.running_log.* 作为运行日志专属配置。
        若配置加载失败，降级使用内置默认值以确保日志子系统不影响主业务启动。
        """
        try:
            from ..config import get_config
            config = get_config()
            logging_cfg = config.get("logging", {})

            # 日志根目录（与 LogManager 共用同一配置项）
            log_root_str = logging_cfg.get("log_root", "~/.pyclaego/logs")
            self._log_root = Path(log_root_str).expanduser().resolve()

            # 运行日志专属配置
            rl_cfg = logging_cfg.get("running_log", {}) or {}
            self._subdir: str = rl_cfg.get("subdir", "running")
            self._line_format: str = rl_cfg.get("format", "[{time}] [{level}] {message}")
            self._time_format: str = rl_cfg.get("time_format", "%Y-%m-%d %H:%M:%S")
            self._encoding: str = rl_cfg.get("encoding", "utf-8")

        except Exception as e:
            # 降级使用默认值
            print(f"[RunningLog] 加载配置失败，使用默认配置: {e}")
            self._log_root = Path("~/.pyclaego/logs").expanduser().resolve()
            self._subdir = "running"
            self._line_format = "[{time}] [{level}] {message}"
            self._time_format = "%Y-%m-%d %H:%M:%S"
            self._encoding = "utf-8"

    def _sanitize_name(self, name: str) -> str:
        """净化 name，替换文件系统不允许的字符为下划线

        Args:
            name: 原始日志标识名

        Returns:
            净化后的安全名称
        """
        invalid_chars = r'/\:*?"<>|'
        for ch in invalid_chars:
            name = name.replace(ch, "_")
        return name

    def _get_log_file(self, name: str, dt: datetime) -> Path:
        """计算日志文件路径

        Args:
            name: 日志标识名（已净化）
            dt:   写入时刻

        Returns:
            日志文件的完整 Path 对象，格式为 {log_root}/{subdir}/{name}-YYYYMMDD-run.log
        """
        log_dir = self._log_root / self._subdir
        safe_name = self._sanitize_name(name)
        date_str = dt.strftime("%Y%m%d")
        filename = f"{safe_name}-{date_str}-run.log"
        return log_dir / filename

    def _format_line(self, level: str, message: str, dt: datetime) -> str:
        """格式化单行日志内容

        Args:
            level:   日志级别字符串（如 INFO、ERROR）
            message: 日志正文
            dt:      写入时刻

        Returns:
            格式化后的日志行（末尾含换行符）
        """
        time_str = dt.strftime(self._time_format)
        line = self._line_format.format(time=time_str, level=level, message=message)
        return line + "\n"

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def log(self, name: str, message: str, level: str = "INFO") -> None:
        """写入运行日志

        根据 name 和当前日期确定目标文件，以追加模式写入一行日志。
        如目录不存在则自动创建。整个写入流程受线程锁保护。

        Args:
            name:    日志标识名，决定写入哪个文件（例如 session_id、模块名）
            message: 日志内容
            level:   日志级别，默认 "INFO"（INFO / WARNING / ERROR / DEBUG）
        """
        with self._lock:
            now = datetime.now()
            log_file = self._get_log_file(name, now)
            # 确保父目录存在
            log_file.parent.mkdir(parents=True, exist_ok=True)
            line = self._format_line(level.upper(), message, now)
            try:
                with open(log_file, "a", encoding=self._encoding) as f:
                    f.write(line)
            except Exception as e:
                # 写入失败不能抛出异常，避免影响主业务
                print(f"[RunningLog] 写入日志失败 ({log_file}): {e}")

    def info(self, name: str, message: str) -> None:
        """写入 INFO 级别日志

        Args:
            name:    日志标识名
            message: 日志内容
        """
        self.log(name, message, "INFO")

    def warning(self, name: str, message: str) -> None:
        """写入 WARNING 级别日志

        Args:
            name:    日志标识名
            message: 日志内容
        """
        self.log(name, message, "WARNING")

    def error(self, name: str, message: str) -> None:
        """写入 ERROR 级别日志

        Args:
            name:    日志标识名
            message: 日志内容
        """
        self.log(name, message, "ERROR")

    def debug(self, name: str, message: str) -> None:
        """写入 DEBUG 级别日志

        Args:
            name:    日志标识名
            message: 日志内容
        """
        self.log(name, message, "DEBUG")

    def get_log_file_path(self, name: str, dt: Optional[datetime] = None) -> Path:
        """获取指定 name 在指定日期（默认今天）对应的日志文件路径

        可用于外部模块查询日志文件位置，无需实际写入。

        Args:
            name: 日志标识名
            dt:   目标日期，默认为当前时刻

        Returns:
            日志文件的完整路径（文件不一定存在）
        """
        return self._get_log_file(name, dt or datetime.now())


# 模块级单例（延迟初始化）
_running_log: Optional[RunningLog] = None


def get_running_log() -> RunningLog:
    """获取 RunningLog 单例实例

    Returns:
        RunningLog: 运行日志管理器单例

    Example:
        >>> from pyclaego.logging import get_running_log
        >>> rlog = get_running_log()
        >>> rlog.info("session_abc", "Session 启动")
    """
    global _running_log

    if _running_log is None:
        _running_log = RunningLog()

    return _running_log
