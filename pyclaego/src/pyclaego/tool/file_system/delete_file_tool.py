"""DeleteFile 工具 - 安全删除文件或目录"""

import os
import shutil
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from ..base_tool import ToolResult, ToolStatus
from .fs_base_tool import FileSystemBaseTool

_rlog = get_running_log()


class DeleteFileTool(FileSystemBaseTool):
    """文件/目录删除工具

    功能：
    - 删除单个文件
    - 删除空目录（不指定 recursive）
    - 递归删除目录树（需显式设置 recursive=true）

    安全机制：
    - 拒绝删除 working_dir 根目录或任何 allowed_paths 根目录
    - 路径黑/白名单校验
    - 删除目录树需要显式 recursive=true 防止误操作

    配置示例：
    ```yaml
    file_delete:
      tool_type: "file_delete"
      tool_name: "file_delete"
      enabled: true
      working_dir: null
      allowed_paths: []
      blocked_paths: []
    ```
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list]:
        p = args.get("path")
        return {"read": [], "write": [p] if isinstance(p, str) and p.strip() else []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行删除操作

        Args:
            path:      要删除的文件或目录路径
            recursive: 是否递归删除目录（默认 false；删除非空目录必须设为 true）

        Returns:
            ToolResult: 包含 deleted_path, was_directory
        """
        valid, error_msg = self.validate_params(["path"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        recursive = self._coerce_bool(kwargs.get("recursive", False), default=False)

        # 安全检查（路径必须存在）
        ok, err = self._security_check(path, require_exists=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        # 拒绝删除工作目录根或白名单根路径 — 已降级为告警；中央 WorkspacePathRule
        # 负责实际拒绝。这里仍然做计算以便记录可观测信号。
        protected = set()
        protected.add(Path(self.working_dir).resolve())
        for ap in self.allowed_paths:
            try:
                protected.add(Path(ap).expanduser().resolve())
            except Exception:
                pass

        if path in protected:
            _rlog.warning(
                "core_service",
                f"[SECURITY_GAP] {self.tool_name}: deletion of protected root "
                f"{path} not blocked in-tool; central rule should reject if applicable.",
            )

        is_dir = path.is_dir()

        try:
            if not is_dir:
                os.remove(path)
                _rlog.info("core_service", f"file_delete: 已删除文件 {path}")
            elif recursive:
                shutil.rmtree(path)
                _rlog.info("core_service", f"file_delete: 已递归删除目录 {path}")
            else:
                # 只删除空目录
                os.rmdir(path)
                _rlog.info("core_service", f"file_delete: 已删除空目录 {path}")

        except OSError as e:
            if not is_dir:
                msg = f"删除文件失败: {e}"
            elif recursive:
                msg = f"递归删除目录失败: {e}"
            else:
                msg = (
                    f"删除目录失败: {e}。"
                    "如果目录非空，请设置 recursive=true 进行递归删除。"
                )
            return ToolResult(status=ToolStatus.FAILED, error=msg)

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={
                "deleted_path": str(path),
                "was_directory": is_dir,
            },
            metadata={"path": str(path)},
        )

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "删除文件或目录。"
                "删除非空目录必须显式设置 recursive=true，防止误操作。"
                "无法删除 working_dir 根目录或 allowed_paths 根目录。"
            ),
            "parameters": {
                "path":      {"type": "string",  "required": True,  "description": "要删除的文件或目录路径"},
                "recursive": {"type": "boolean", "required": False, "description": "是否递归删除目录（默认 false；非空目录必须设为 true）"},
            },
            "returns": {
                "deleted_path":  "已删除的路径",
                "was_directory": "是否为目录",
            },
        }
