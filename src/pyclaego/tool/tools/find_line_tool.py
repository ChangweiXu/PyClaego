"""FindLine 工具 - 在单个文件中精确定位关键词出现位置"""

import re
from pathlib import Path
from typing import Dict, Any, List

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class FindLineTool(FileSystemBaseTool):
    """关键词行列定位工具

    功能：
    - 在单个文件中搜索关键词，返回每处匹配的行号和列号
    - 支持纯字符串或正则表达式
    - 专为"定位后编辑"工作流设计：返回精确坐标，
      便于 Agent 在 file_edit 中构造足够唯一的 old_str

    与 SearchTextTool 的区别：
    - 仅处理单个文件（不支持目录递归）
    - 返回精确列号（column）
    - 无 context_lines，输出更紧凑
    - 轻量，专用于"先定位，再编辑"的链式调用

    配置示例：
    ```yaml
    find_line:
      tool_type: "find_line"
      tool_name: "find_line"
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
        """在文件中定位关键词

        Args:
            path:     目标文件路径（必须是文件，不支持目录）
            keyword:  搜索关键词或正则表达式
            is_regex: 是否按正则搜索（默认 false）
            encoding: 文件编码（默认 utf-8）

        Returns:
            ToolResult: 包含 occurrences 列表（line_number, column, line_content）
                        及 total_count, path
        """
        valid, error_msg = self.validate_params(["path", "keyword"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        keyword: str = kwargs["keyword"]
        is_regex: bool = self._coerce_bool(kwargs.get("is_regex", False), default=False)
        encoding: str = kwargs.get("encoding", "utf-8")

        ok, err = self._security_check(path, require_exists=True, must_be_file=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        ok, err = self._check_file_size(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        # 编译正则
        compiled: re.Pattern | None = None
        if is_regex:
            try:
                compiled = re.compile(keyword)
            except re.error as e:
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error=f"正则表达式编译失败: {e}",
                )

        try:
            with open(path, "r", encoding=encoding, errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult(status=ToolStatus.FAILED, error=f"读取文件失败: {e}")

        occurrences: List[Dict[str, Any]] = []

        for line_no, raw_line in enumerate(lines, start=1):
            line = raw_line.rstrip("\n\r")
            if is_regex and compiled:
                for m in compiled.finditer(line):
                    occurrences.append({
                        "line_number":  line_no,
                        "column":       m.start() + 1,   # 1-based
                        "line_content": line,
                        "match":        m.group(0),
                    })
            else:
                # 纯字符串，找所有位置
                start = 0
                while True:
                    idx = line.find(keyword, start)
                    if idx == -1:
                        break
                    occurrences.append({
                        "line_number":  line_no,
                        "column":       idx + 1,          # 1-based
                        "line_content": line,
                    })
                    start = idx + len(keyword)

        _rlog.info(
            "core_service",
            f"find_line: {path} | keyword={keyword!r} | found={len(occurrences)}",
        )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={
                "occurrences": occurrences,
                "total_count": len(occurrences),
                "path":        str(path),
            },
            metadata={
                "path":     str(path),
                "keyword":  keyword,
                "is_regex": is_regex,
            },
        )

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "在单个文件中精确定位关键词，返回每处匹配的行号和列号。"
                "支持纯字符串或正则表达式。"
                "专为「定位后编辑」工作流设计：先用 find_line 获取行号，"
                "再用 read_file 读取对应行区域，构造唯一的 old_str，"
                "最后用 file_edit 执行精确替换。"
            ),
            "parameters": {
                "path":     {"type": "string",  "required": True,  "description": "目标文件路径（必须是文件）"},
                "keyword":  {"type": "string",  "required": True,  "description": "搜索关键词或正则表达式"},
                "is_regex": {"type": "boolean", "required": False, "description": "是否按正则搜索（默认 false）"},
                "encoding": {"type": "string",  "required": False, "description": "文件编码（默认 utf-8）"},
            },
            "returns": {
                "occurrences": "匹配列表，每项含 line_number（1-based）、column（1-based）、line_content、match（正则模式时）",
                "total_count": "匹配总数",
                "path":        "文件绝对路径",
            },
        }
