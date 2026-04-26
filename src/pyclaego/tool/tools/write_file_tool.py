"""WriteFile 工具 - 安全地写入本地文本文件"""

import os
from pathlib import Path
from typing import Dict, Any

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class WriteFileTool(FileSystemBaseTool):
    """文件写入工具
    
    功能：
    - 覆盖或追加写入文本文件
    - 自动创建父目录（可配置）
    - 严格的路径安全校验
    
    配置示例：
    ```yaml
    write_file:
      tool_type: "write_file"
      tool_name: "write_file"
      enabled: true
      working_dir: null
      max_file_size: 10485760
      allowed_paths: []
      blocked_paths: []
      auto_mkdir: true
      forbid_write_outside_allowed: false
    ```
    """

    # 写入文件，修改文件系统；并发写同一文件存在竞争风险
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path")
        return {"read": [], "write": [p] if isinstance(p, str) and p.strip() else []}

    def __init__(self, tool_config: Dict[str, Any]):
        super().__init__(tool_config)
        self.auto_mkdir = tool_config.get("auto_mkdir", True)
        self.forbid_write_outside_allowed = tool_config.get(
            "forbid_write_outside_allowed", False
        )

    async def execute(self, **kwargs) -> ToolResult:
        """执行文件写入
        
        Args:
            path:        目标文件路径
            content:     要写入的内容
            encoding:    文件编码（默认 utf-8）
            mode:        写入模式："write"（覆盖）、"append"（追加）、"insert"（行插入）
            line_number: mode="insert" 时在第几行之前插入（1-based，默认 1 即文件头部）
            
        Returns:
            ToolResult: 包含 written_bytes, path, mode
        """
        valid, error_msg = self.validate_params(["path", "content"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        content = kwargs["content"]
        encoding = kwargs.get("encoding", "utf-8")
        mode = kwargs.get("mode", "write")

        if mode not in ("write", "append", "insert"):
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"不支持的写入模式: {mode}，仅支持 write / append / insert"
            )

        # 安全检查（写操作不需要文件已存在）
        ok, err = self._security_check(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        # 如果启用了白名单外写入禁止，且未配置白名单 — 已降级为告警，由中央
        # WorkspacePathRule 强制执行；此处仅留下可观测信号。
        if self.forbid_write_outside_allowed and not self.allowed_paths:
            _rlog.warning(
                "core_service",
                f"[SECURITY_GAP] {self.tool_name}: forbid_write_outside_allowed=true "
                f"but allowed_paths is empty; in-tool check no longer blocks. "
                f"Rely on central WorkspacePathRule.",
            )

        # 内容大小检查（编码后字节数）
        content_bytes = content.encode(encoding)
        if len(content_bytes) > self.max_file_size:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"写入内容大小 ({len(content_bytes)} 字节) 超出限制 ({self.max_file_size} 字节)"
            )

        try:
            _rlog.info("core_service", f"写入文件: {path} (mode={mode}, encoding={encoding})")

            if self.auto_mkdir:
                path.parent.mkdir(parents=True, exist_ok=True)

            if mode == "insert":
                line_number = max(1, self._coerce_int(kwargs.get("line_number", 1), default=1))
                # 读取现有行（文件不存在时视为空文件）
                if path.exists():
                    with open(path, "r", encoding=encoding, errors="replace") as f:
                        existing_lines = f.readlines()
                else:
                    existing_lines = []
                # 插入位置：line_number 之前（0-based index = line_number - 1）
                insert_idx = min(line_number - 1, len(existing_lines))
                # 确保插入内容以换行符结尾
                insert_content = content if content.endswith("\n") else content + "\n"
                existing_lines.insert(insert_idx, insert_content)
                with open(path, "w", encoding=encoding) as f:
                    f.writelines(existing_lines)
            else:
                open_mode = "a" if mode == "append" else "w"
                with open(path, open_mode, encoding=encoding) as f:
                    f.write(content)

            _rlog.info("core_service", f"文件写入成功: {path}, 字节数={len(content_bytes)}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "written_bytes": len(content_bytes),
                    "path": str(path),
                    "mode": mode,
                },
                metadata={
                    "path": str(path),
                    "encoding": encoding,
                    "mode": mode,
                }
            )

        except Exception as e:
            error_msg = f"写入文件异常: {path}, 错误: {str(e)}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "安全地写入或追加本地文本文件",
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "目标文件路径"
                },
                "content": {
                    "type": "string",
                    "required": True,
                    "description": "要写入的文本内容"
                },
                "encoding": {
                    "type": "string",
                    "required": False,
                    "description": "文件编码（默认 utf-8）"
                },
                "mode": {
                    "type": "string",
                    "required": False,
                    "description": "写入模式: write（覆盖）、append（追加）、insert（行插入），默认 write"
                },
                "line_number": {
                    "type": "integer",
                    "required": False,
                    "description": "mode=insert 时在第几行之前插入（1-based，默认 1 即文件头部）"
                }
            },
            "returns": {
                "written_bytes": "实际写入字节数",
                "path": "最终绝对路径",
                "mode": "写入模式"
            },
            "examples": [
                {
                    "path": "notes.txt",
                    "content": "Hello World",
                    "description": "覆盖写入 notes.txt"
                },
                {
                    "path": "log.txt",
                    "content": "New line\n",
                    "mode": "append",
                    "description": "追加一行到 log.txt"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
