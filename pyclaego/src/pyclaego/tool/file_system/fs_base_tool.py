"""文件系统工具基类 - 提供统一的路径解析与安全校验"""

import os
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from ..base_tool import BaseTool

_rlog = get_running_log()


class FileSystemBaseTool(BaseTool):
    """文件系统工具基类
    
    为所有涉及本地文件系统操作的工具提供：
    - 工作目录解析
    - 白名单/黑名单路径安全检查
    - 最大文件尺寸限制
    """

    # 安全兜底值：文件系统操作默认视为非只读、不可并发
    # 具体子类（ReadFileTool、ListDirectoryTool 等）应显式覆盖
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def __init__(self, tool_config: dict[str, Any]):
        """初始化文件系统工具
        
        Args:
            tool_config: 工具配置字典
        """
        super().__init__(tool_config)

        # 工作目录（未配置则使用当前工作目录）
        self.working_dir = tool_config.get("working_dir") or os.getcwd()

        # 允许访问的路径白名单（空列表表示不启用白名单限制，但仍受黑名单约束）
        self.allowed_paths = tool_config.get("allowed_paths", [])

        # 禁止访问的路径黑名单
        self.blocked_paths = tool_config.get("blocked_paths", [])

        # 最大允许操作的文件大小（默认 10MB）
        self.max_file_size = tool_config.get("max_file_size", 10 * 1024 * 1024)

    def _resolve_path(self, path: str) -> Path:
        """将传入路径解析为绝对路径
        
        Args:
            path: 原始路径字符串
            
        Returns:
            Path: 解析后的绝对路径
        """
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(self.working_dir) / p
        return p.resolve()

    def _security_check(
        self,
        path: Path,
        require_exists: bool = False,
        must_be_file: bool = False,
        must_be_dir: bool = False
    ) -> tuple[bool, str]:
        """执行路径安全检查
        
        Args:
            path: 已解析的绝对路径
            require_exists: 是否要求路径必须存在
            must_be_file: 是否要求路径必须是文件
            must_be_dir: 是否要求路径必须是目录
            
        Returns:
            Tuple[bool, str]: (是否通过, 错误信息)
        """
        # 存在性检查
        if require_exists and not path.exists():
            return False, f"路径不存在: {path}"
        
        if must_be_file and path.exists() and not path.is_file():
            return False, f"路径不是文件: {path}"
        
        if must_be_dir and path.exists() and not path.is_dir():
            return False, f"路径不是目录: {path}"

        # 黑名单检查（已降级为告警，由中央 WorkspacePathRule 强制；保留作为深度防御）
        for blocked_raw in self.blocked_paths:
            try:
                blocked = Path(blocked_raw).expanduser().resolve()
                if path == blocked or self._is_under(path, blocked):
                    _rlog.warning(
                        "core_service",
                        f"[SECURITY_GAP] {self.tool_name}: path {path} is inside "
                        f"configured blocked_paths entry {blocked} but allowed by "
                        f"in-tool check; central rule should reject if applicable.",
                    )
                    break
            except Exception:
                pass

        # 白名单检查（已降级为告警，由中央 WorkspacePathRule 强制；保留作为深度防御）
        if self.allowed_paths:
            in_allowed = False
            for allowed_raw in self.allowed_paths:
                try:
                    allowed = Path(allowed_raw).expanduser().resolve()
                    if path == allowed or self._is_under(path, allowed):
                        in_allowed = True
                        break
                except Exception:
                    pass
            if not in_allowed:
                _rlog.warning(
                    "core_service",
                    f"[SECURITY_GAP] {self.tool_name}: path {path} is outside "
                    f"configured allowed_paths {self.allowed_paths} but allowed by "
                    f"in-tool check; central rule should reject if applicable.",
                )

        return True, ""

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """对 output dict 中所有顶层字符串值进行脱敏（默认实现，供文件系统子类继承）。

        ReadFileTool（content 字段）和 WriteFileTool（path 字段）直接继承此实现。
        对 entries 等嵌套结构有特殊需求的子类应重写此方法。

        Args:
            raw_output: execute() 返回的 output 字典
            path_mask_map: 真实路径 -> 占位符的映射字典

        Returns:
            脱敏后的 output 字典
        """
        if not isinstance(raw_output, dict):
            return raw_output
        masked = {}
        for key, value in raw_output.items():
            if isinstance(value, str):
                masked[key] = self._mask_string(value, path_mask_map)
            else:
                masked[key] = value
        return masked

    def _check_file_size(self, path: Path) -> tuple[bool, str]:
        """检查文件大小是否超出限制
        
        Args:
            path: 文件路径
            
        Returns:
            Tuple[bool, str]: (是否通过, 错误信息)
        """
        if not path.exists() or not path.is_file():
            return True, ""
        
        try:
            size = path.stat().st_size
            if size > self.max_file_size:
                return False, (
                    f"文件大小 ({size} 字节) 超出限制 ({self.max_file_size} 字节): "
                    f"{path}"
                )
        except OSError as e:
            return False, f"无法读取文件大小: {path}, 错误: {e}"
        
        return True, ""
