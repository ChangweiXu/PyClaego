"""ListDirectory 工具 - 安全地列出目录内容"""

import fnmatch
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class ListDirectoryTool(FileSystemBaseTool):
    """目录阅览工具
    
    功能：
    - 列出指定目录下的文件与文件夹
    - 支持递归遍历
    - 支持 glob 模式过滤
    - 返回条目类型、大小、修改时间
    
    配置示例：
    ```yaml
    list_directory:
      tool_type: "list_directory"
      tool_name: "list_directory"
      enabled: true
      working_dir: null
      allowed_paths: []
      blocked_paths: []
    ```
    """

    # 仅列出目录，不修改任何状态；多个并发列目录安全
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path", ".")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行目录列出
        
        Args:
            path: 目标目录路径（默认当前工作目录）
            recursive: 是否递归列出子目录（默认 false）
            pattern: glob 过滤模式，如 "*.py"（可选）
            
        Returns:
            ToolResult: 包含 entries, total_count, path
        """
        path = self._resolve_path(kwargs.get("path", "."))
        recursive = self._coerce_bool(kwargs.get("recursive", False), default=False)
        pattern = kwargs.get("pattern", None)

        # 安全检查
        ok, err = self._security_check(path, require_exists=True, must_be_dir=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        try:
            _rlog.info("core_service", f"列出目录: {path} (recursive={recursive}, pattern={pattern})")

            entries: List[Dict[str, Any]] = []
            iterator = path.rglob("*") if recursive else path.iterdir()

            for entry in iterator:
                # 对递归产生的子路径再做一次安全检查
                ok, _ = self._security_check(entry.resolve())
                if not ok:
                    continue

                name = str(entry.relative_to(path))
                if pattern and not fnmatch.fnmatch(entry.name, pattern):
                    continue

                stat = entry.stat()
                entry_type = "file"
                if entry.is_dir():
                    entry_type = "dir"
                elif entry.is_symlink():
                    entry_type = "link"

                entries.append({
                    "name": name,
                    "type": entry_type,
                    "size": stat.st_size if entry.is_file() else 0,
                    "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })

            # 按目录在前、文件在后的顺序排序，同名再按字母序
            entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))

            _rlog.info("core_service", f"目录列出成功: {path}, 条目数={len(entries)}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "entries": entries,
                    "total_count": len(entries),
                    "path": str(path),
                },
                metadata={
                    "path": str(path),
                    "recursive": recursive,
                    "pattern": pattern,
                }
            )

        except Exception as e:
            error_msg = f"列出目录异常: {path}, 错误: {str(e)}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        """对目录列出结果中的路径进行脱敏。

        脱敏字段：
        - output["path"]：目录的绝对路径
        - output["entries"][*]["name"]：各条目的相对路径名（递归模式下为相对路径）

        Args:
            raw_output: execute() 返回的 output 字典
            path_mask_map: 真实路径 -> 占位符的映射字典

        Returns:
            脱敏后的 output 字典
        """
        if not isinstance(raw_output, dict):
            return raw_output
        masked = dict(raw_output)
        if "path" in masked and isinstance(masked["path"], str):
            masked["path"] = self._mask_string(masked["path"], path_mask_map)
        if "entries" in masked and isinstance(masked["entries"], list):
            masked_entries = []
            for entry in masked["entries"]:
                if isinstance(entry, dict):
                    masked_entry = dict(entry)
                    if "name" in masked_entry and isinstance(masked_entry["name"], str):
                        masked_entry["name"] = self._mask_string(masked_entry["name"], path_mask_map)
                    masked_entries.append(masked_entry)
                else:
                    masked_entries.append(entry)
            masked["entries"] = masked_entries
        return masked

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "安全地列出目录内容，支持递归与过滤",
            "parameters": {
                "path": {
                    "type": "string",
                    "required": False,
                    "description": "目标目录路径（默认当前目录）"
                },
                "recursive": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否递归列出子目录（默认 false）"
                },
                "pattern": {
                    "type": "string",
                    "required": False,
                    "description": "glob 过滤模式，如 *.py（可选）"
                }
            },
            "returns": {
                "entries": "条目列表（含 name, type, size, modified_time）",
                "total_count": "条目总数",
                "path": "解析后的绝对目录路径"
            },
            "examples": [
                {
                    "path": "src",
                    "description": "列出 src 目录下的文件和文件夹"
                },
                {
                    "path": "src",
                    "recursive": True,
                    "pattern": "*.py",
                    "description": "递归列出 src 下所有 .py 文件"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
