"""CopyMove 工具 - 复制或移动文件/目录"""

import shutil
from typing import Any

from ...logging import get_running_log
from ..base_tool import ToolResult, ToolStatus
from .fs_base_tool import FileSystemBaseTool

_rlog = get_running_log()


class CopyMoveTool(FileSystemBaseTool):
    """文件/目录复制与移动工具

    功能：
    - 复制文件（保留元数据：copy2）
    - 复制目录树（copytree）
    - 移动文件或目录（支持跨设备）

    安全机制：
    - 对 source 和 destination 均执行路径安全校验
    - 默认 overwrite=false，目标已存在时拒绝操作

    配置示例：
    ```yaml
    copy_move:
      tool_type: "copy_move"
      tool_name: "copy_move"
      enabled: true
      working_dir: null
      allowed_paths: []
      blocked_paths: []
    ```
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list]:
        src = args.get("source")
        dst = args.get("destination")
        action = (args.get("action") or "copy").strip().lower() if isinstance(args.get("action"), str) else "copy"
        reads = [src] if isinstance(src, str) and src.strip() else []
        writes = [dst] if isinstance(dst, str) and dst.strip() else []
        # "move" also removes the source — treat it as a write to source.
        if action == "move" and reads:
            writes = list(writes) + reads
            reads = []
        return {"read": reads, "write": writes}

    async def execute(self, **kwargs) -> ToolResult:
        """执行复制或移动

        Args:
            source:      源文件/目录路径
            destination: 目标路径
            action:      操作类型："copy" 或 "move"
            overwrite:   目标已存在时是否覆盖（默认 false）

        Returns:
            ToolResult: 包含 source, destination, action, is_directory
        """
        valid, error_msg = self.validate_params(["source", "destination", "action"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        src = self._resolve_path(kwargs["source"])
        dst = self._resolve_path(kwargs["destination"])
        action: str = kwargs["action"].strip().lower()
        overwrite: bool = self._coerce_bool(kwargs.get("overwrite", False), default=False)

        if action not in ("copy", "move"):
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"不支持的操作: {action!r}，仅支持 'copy' 或 'move'",
            )

        # 安全检查 source（必须存在）
        ok, err = self._security_check(src, require_exists=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=f"source: {err}")

        # 安全检查 destination（无需存在，但路径合法）
        ok, err = self._security_check(dst)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=f"destination: {err}")

        # 目标存在时的处理
        if dst.exists() and not overwrite:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=(
                    f"目标路径已存在: {dst}。"
                    "如需覆盖，请设置 overwrite=true。"
                ),
            )

        is_dir = src.is_dir()

        try:
            # 自动创建目标父目录
            dst.parent.mkdir(parents=True, exist_ok=True)

            if action == "copy":
                if is_dir:
                    if dst.exists() and overwrite:
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                _rlog.info("core_service", f"copy_move(copy): {src} -> {dst}")
            else:  # move
                shutil.move(str(src), str(dst))
                _rlog.info("core_service", f"copy_move(move): {src} -> {dst}")

        except Exception as e:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"{action} 操作失败: {e}",
            )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={
                "source":      str(src),
                "destination": str(dst),
                "action":      action,
                "is_directory": is_dir,
            },
            metadata={"source": str(src), "destination": str(dst)},
        )

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "复制或移动文件/目录。"
                "action='copy' 复制文件（保留元数据）或整个目录树；"
                "action='move' 移动文件或目录（支持跨设备）。"
                "默认不覆盖已存在的目标，需要覆盖时设置 overwrite=true。"
            ),
            "parameters": {
                "source":      {"type": "string",  "required": True,  "description": "源文件或目录路径"},
                "destination": {"type": "string",  "required": True,  "description": "目标路径"},
                "action":      {"type": "string",  "required": True,  "description": "'copy' 或 'move'"},
                "overwrite":   {"type": "boolean", "required": False, "description": "目标存在时是否覆盖（默认 false）"},
            },
            "returns": {
                "source":       "源路径（绝对）",
                "destination":  "目标路径（绝对）",
                "action":       "执行的操作",
                "is_directory": "是否为目录",
            },
        }
