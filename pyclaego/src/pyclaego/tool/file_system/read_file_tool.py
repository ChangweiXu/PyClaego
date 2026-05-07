"""ReadFile 工具 - 安全地读取本地文本文件内容"""

from typing import Any

from ...logging import get_running_log
from ..base_tool import ToolResult, ToolStatus
from .fs_base_tool import FileSystemBaseTool

_rlog = get_running_log()


class ReadFileTool(FileSystemBaseTool):
    """文件读取工具
    
    功能：
    - 读取指定文本文件内容
    - 支持按行偏移与行数限制分页
    - 自动校验路径安全与文件大小限制
    
    配置示例：
    ```yaml
    read_file:
      tool_type: "read_file"
      tool_name: "read_file"
      enabled: true
      working_dir: null
      max_file_size: 10485760   # 10MB
      allowed_paths: []
      blocked_paths: []
    ```
    """

    # 仅读取文件，不修改任何状态；多个并发读取安全
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list]:
        p = args.get("path")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行文件读取
        
        Args:
            path: 目标文件路径（相对路径基于 working_dir 解析）
            encoding: 文件编码（默认 utf-8）
            offset: 起始行号，1-based（默认 1）
            limit: 最大读取行数（默认 1000）
            
        Returns:
            ToolResult: 包含 content, total_lines, read_lines, file_size, has_more
        """
        valid, error_msg = self.validate_params(["path"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        encoding = kwargs.get("encoding", "utf-8")
        offset = max(1, self._coerce_int(kwargs.get("offset", 1), default=1))
        limit = max(1, self._coerce_int(kwargs.get("limit", 10485760), default=10485760))  # 设置一个非常大的默认值，实际读取时会受 max_file_size 限制

        # 安全检查
        ok, err = self._security_check(path, require_exists=True, must_be_file=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        ok, err = self._check_file_size(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        try:
            _rlog.info("core_service", f"读取文件: {path} (encoding={encoding}, offset={offset}, limit={limit})")

            # 快速统计总行数
            total_lines = 0
            with open(path, "rb") as f:
                for _ in f:
                    total_lines += 1

            # 按指定范围读取
            read_lines = 0
            lines = []
            with open(path, encoding=encoding, errors="replace") as f:
                for idx, line in enumerate(f, start=1):
                    if idx < offset:
                        continue
                    if read_lines >= limit:
                        break
                    lines.append(line)
                    read_lines += 1

            content = "".join(lines)
            has_more = (offset + read_lines - 1) < total_lines

            _rlog.info("core_service",
                f"文件读取成功: {path}, 总行数={total_lines}, 读取行数={read_lines}"
            )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "content": content,
                    "total_lines": total_lines,
                    "read_lines": read_lines,
                    "file_size": path.stat().st_size,
                    "has_more": has_more,
                },
                metadata={
                    "path": str(path),
                    "encoding": encoding,
                    "offset": offset,
                    "limit": limit,
                }
            )

        except UnicodeDecodeError as e:
            error_msg = f"文件解码失败（可能不是文本文件）: {path}, 错误: {e}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)
        except Exception as e:
            error_msg = f"读取文件异常: {path}, 错误: {e!s}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "安全地读取本地文本文件内容，支持分页",
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "目标文件路径"
                },
                "encoding": {
                    "type": "string",
                    "required": False,
                    "description": "文件编码（默认 utf-8）"
                },
                "offset": {
                    "type": "integer",
                    "required": False,
                    "description": "起始行号，从 1 开始（默认 1）"
                },
                "limit": {
                    "type": "integer",
                    "required": False,
                    "description": "最大读取行数（默认 1000）"
                }
            },
            "returns": {
                "content": "文件文本内容",
                "total_lines": "文件总行数",
                "read_lines": "实际读取行数",
                "file_size": "文件字节大小",
                "has_more": "是否还有更多内容"
            },
            "examples": [
                {
                    "path": "src/main.py",
                    "description": "读取 src/main.py 的前 1000 行"
                },
                {
                    "path": "README.md",
                    "offset": 1,
                    "limit": 50,
                    "description": "读取 README.md 的前 50 行"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
