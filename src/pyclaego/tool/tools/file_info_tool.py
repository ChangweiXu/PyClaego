"""FileInfo 工具 - 获取文件元信息及结构大纲"""

import re
import mimetypes
from pathlib import Path
from typing import Dict, Any, List, Optional

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class FileInfoTool(FileSystemBaseTool):
    """文件预处理与元信息工具

    功能：
    - 统计文件行数、字符数
    - 估算 Token 数（字符数 / 4，可用于决定是否需要分段读取）
    - 检测 MIME 类型
    - 提取代码/文档结构大纲（正则，无需安装额外依赖）
      - .py  → class / def 声明
      - .md  → 标题（# ## ###）
      - .js/.ts/.jsx/.tsx → class / function / export 声明

    配置示例：
    ```yaml
    file_info:
      tool_type: "file_info"
      tool_name: "file_info"
      enabled: true
      working_dir: null
      max_file_size: 10485760
      allowed_paths: []
      blocked_paths: []
    ```
    """

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """获取文件元信息

        Args:
            path:     目标文件路径
            outline:  是否提取结构大纲（默认 false）
            encoding: 文件编码（默认 utf-8）

        Returns:
            ToolResult: 包含 path, file_size, line_count, char_count,
                        token_estimate, mime_type, outline（可选）
        """
        valid, error_msg = self.validate_params(["path"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        outline: bool = self._coerce_bool(kwargs.get("outline", False), default=False)
        encoding: str = kwargs.get("encoding", "utf-8")

        ok, err = self._security_check(path, require_exists=True, must_be_file=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        ok, err = self._check_file_size(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        file_size = path.stat().st_size
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

        # 读取文件内容统计
        try:
            with open(path, "r", encoding=encoding, errors="replace") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(status=ToolStatus.FAILED, error=f"读取文件失败: {e}")

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        char_count = len(content)
        token_estimate = max(1, char_count // 4)

        output: Dict[str, Any] = {
            "path":           str(path),
            "file_size":      file_size,
            "line_count":     line_count,
            "char_count":     char_count,
            "token_estimate": token_estimate,
            "mime_type":      mime_type,
        }

        if outline:
            output["outline"] = self._extract_outline(path.suffix.lower(), content)

        _rlog.info("core_service", f"file_info: {path} | lines={line_count} | tokens~{token_estimate}")

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=output,
            metadata={"path": str(path), "encoding": encoding},
        )

    # ------------------------------------------------------------------
    # Outline extraction (regex-based, no AST)
    # ------------------------------------------------------------------

    def _extract_outline(self, suffix: str, content: str) -> List[Dict[str, Any]]:
        """从文件内容提取结构大纲"""
        if suffix == ".py":
            return self._outline_python(content)
        elif suffix in (".md", ".markdown"):
            return self._outline_markdown(content)
        elif suffix in (".js", ".ts", ".jsx", ".tsx"):
            return self._outline_js_ts(content)
        else:
            return []

    def _outline_python(self, content: str) -> List[Dict[str, Any]]:
        pattern = re.compile(r"^(class|def)\s+(\w+)", re.MULTILINE)
        result = []
        for i, line in enumerate(content.splitlines(), start=1):
            m = pattern.match(line)
            if m:
                indent = len(line) - len(line.lstrip())
                result.append({
                    "line":   i,
                    "kind":   m.group(1),
                    "name":   m.group(2),
                    "indent": indent,
                })
        return result

    def _outline_markdown(self, content: str) -> List[Dict[str, Any]]:
        pattern = re.compile(r"^(#{1,6})\s+(.+)")
        result = []
        for i, line in enumerate(content.splitlines(), start=1):
            m = pattern.match(line)
            if m:
                result.append({
                    "line":  i,
                    "level": len(m.group(1)),
                    "title": m.group(2).strip(),
                })
        return result

    def _outline_js_ts(self, content: str) -> List[Dict[str, Any]]:
        patterns = [
            re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+(\w+)"),
            re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
            re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\("),
            re.compile(r"^export\s+(const|let|var|function|class|default)\b"),
        ]
        result = []
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.lstrip()
            for pat in patterns:
                m = pat.match(stripped)
                if m:
                    result.append({
                        "line": i,
                        "declaration": line.strip()[:120],
                    })
                    break
        return result

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "获取文件元信息：行数、字符数、Token 估算（字符数÷4）、MIME 类型。"
                "设置 outline=true 可提取代码/文档结构大纲（支持 .py、.md、.js/.ts）。"
                "适合在读取大文件前先评估大小，决定是否需要分段读取。"
            ),
            "parameters": {
                "path":     {"type": "string",  "required": True,  "description": "目标文件路径"},
                "outline":  {"type": "boolean", "required": False, "description": "是否提取结构大纲（默认 false）"},
                "encoding": {"type": "string",  "required": False, "description": "文件编码（默认 utf-8）"},
            },
            "returns": {
                "path":           "文件绝对路径",
                "file_size":      "文件字节数",
                "line_count":     "总行数",
                "char_count":     "总字符数",
                "token_estimate": "Token 估算（字符数÷4）",
                "mime_type":      "推测的 MIME 类型",
                "outline":        "结构大纲列表（仅 outline=true 时包含）",
            },
        }
