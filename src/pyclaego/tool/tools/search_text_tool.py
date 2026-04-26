"""SearchText 工具 - 在文件或目录中搜索文本"""

import re
from pathlib import Path
from typing import Dict, Any, List

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class SearchTextTool(FileSystemBaseTool):
    """文本查找工具
    
    功能：
    - 在单个文件或整个目录中搜索文本
    - 支持纯字符串匹配或正则表达式
    - 支持显示匹配行前后上下文
    - 自动跳过超大文件与二进制文件
    
    配置示例：
    ```yaml
    search_text:
      tool_type: "search_text"
      tool_name: "search_text"
      enabled: true
      working_dir: null
      max_file_size: 10485760
      allowed_paths: []
      blocked_paths: []
    ```
    """

    # 仅读取和匹配文件内容，不修改任何状态；多个并发搜索安全
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行文本搜索
        
        Args:
            path: 目标文件或目录路径
            pattern: 搜索字符串或正则表达式
            is_regex: 是否按正则搜索（默认 false）
            recursive: 当 path 为目录时是否递归搜索（默认 true）
            context_lines: 匹配行前后附带的上下文行数（默认 0）
            encoding: 文件编码（默认 utf-8）
            
        Returns:
            ToolResult: 包含 matches, match_count, searched_files
        """
        valid, error_msg = self.validate_params(["path", "pattern"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        search_pattern = kwargs["pattern"]
        is_regex = self._coerce_bool(kwargs.get("is_regex", False), default=False)
        recursive = self._coerce_bool(kwargs.get("recursive", True), default=True)
        context_lines = max(0, self._coerce_int(kwargs.get("context_lines", 0), default=0))
        encoding = kwargs.get("encoding", "utf-8")

        # 安全检查
        ok, err = self._security_check(path, require_exists=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        try:
            compiled = re.compile(search_pattern) if is_regex else None
        except re.error as e:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"正则表达式编译失败: {e}"
            )

        try:
            _rlog.info("core_service",
                f"搜索文本: path={path}, pattern={search_pattern}, is_regex={is_regex}"
            )

            matches: List[Dict[str, Any]] = []
            searched_files = 0

            if path.is_file():
                files_to_search = [path]
            else:
                files_to_search = []
                iterator = path.rglob("*") if recursive else path.iterdir()
                for entry in iterator:
                    if entry.is_file():
                        # 安全检查
                        ok, _ = self._security_check(entry.resolve())
                        if ok:
                            files_to_search.append(entry)

            for file_path in files_to_search:
                ok, err = self._check_file_size(file_path)
                if not ok:
                    _rlog.warning("core_service", f"跳过超大文件: {file_path}, 原因: {err}")
                    continue

                if self._is_binary(file_path):
                    _rlog.warning("core_service", f"跳过二进制文件: {file_path}")
                    continue

                file_matches = self._search_in_file(
                    file_path, search_pattern, is_regex, compiled,
                    context_lines, encoding
                )
                searched_files += 1
                matches.extend(file_matches)

            _rlog.info(
                "core_service",
                f"搜索完成: 匹配数={len(matches)}, 搜索文件数={searched_files}",
            )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "matches": matches,
                    "match_count": len(matches),
                    "searched_files": searched_files,
                },
                metadata={
                    "path": str(path),
                    "pattern": search_pattern,
                    "is_regex": is_regex,
                    "recursive": recursive,
                    "context_lines": context_lines,
                }
            )

        except Exception as e:
            error_msg = f"搜索文本异常: {path}, 错误: {str(e)}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def _is_binary(self, file_path: Path, sample_size: int = 8192) -> bool:
        """简单判断文件是否为二进制文件"""
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(sample_size)
                return b"\x00" in chunk
        except Exception:
            return True

    def _search_in_file(
        self,
        file_path: Path,
        pattern: str,
        is_regex: bool,
        compiled: re.Pattern,
        context_lines: int,
        encoding: str
    ) -> List[Dict[str, Any]]:
        """在单个文件中搜索"""
        matches = []
        try:
            with open(file_path, "r", encoding=encoding, errors="replace") as f:
                all_lines = list(f)
        except Exception:
            return matches

        for idx, line in enumerate(all_lines, start=1):
            line_stripped = line.rstrip("\n\r")
            found = False
            if is_regex and compiled:
                found = compiled.search(line_stripped) is not None
            else:
                found = pattern in line_stripped

            if found:
                start = max(0, idx - context_lines - 1)
                end = min(len(all_lines), idx + context_lines)
                context_before = [l.rstrip("\n\r") for l in all_lines[start:idx - 1]]
                context_after = [l.rstrip("\n\r") for l in all_lines[idx:end]]

                matches.append({
                    "file": str(file_path),
                    "line_number": idx,
                    "line_content": line_stripped,
                    "context_before": context_before,
                    "context_after": context_after,
                })

        return matches

    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        """对文本搜索结果中的路径及文本内容进行脱敏。

        脱敏字段：
        - matches[*].file：匹配文件的绝对路径
        - matches[*].line_content：匹配行内容（可能包含路径字符串）
        - matches[*].context_before：前置上下文行列表
        - matches[*].context_after：后置上下文行列表

        Args:
            raw_output: execute() 返回的 output 字典
            path_mask_map: 真实路径 -> 占位符的映射字典

        Returns:
            脱敏后的 output 字典
        """
        if not isinstance(raw_output, dict):
            return raw_output
        masked = dict(raw_output)
        if "matches" in masked and isinstance(masked["matches"], list):
            masked_matches = []
            for match in masked["matches"]:
                if not isinstance(match, dict):
                    masked_matches.append(match)
                    continue
                m = dict(match)
                if "file" in m:
                    m["file"] = self._mask_string(m["file"], path_mask_map)
                if "line_content" in m:
                    m["line_content"] = self._mask_string(m["line_content"], path_mask_map)
                if "context_before" in m and isinstance(m["context_before"], list):
                    m["context_before"] = [
                        self._mask_string(line, path_mask_map)
                        for line in m["context_before"]
                    ]
                if "context_after" in m and isinstance(m["context_after"], list):
                    m["context_after"] = [
                        self._mask_string(line, path_mask_map)
                        for line in m["context_after"]
                    ]
                masked_matches.append(m)
            masked["matches"] = masked_matches
        return masked

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "在文件或目录中搜索文本，支持正则与上下文",
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "目标文件或目录路径"
                },
                "pattern": {
                    "type": "string",
                    "required": True,
                    "description": "搜索字符串或正则表达式"
                },
                "is_regex": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否按正则搜索（默认 false）"
                },
                "recursive": {
                    "type": "boolean",
                    "required": False,
                    "description": "当 path 为目录时是否递归搜索（默认 true）"
                },
                "context_lines": {
                    "type": "integer",
                    "required": False,
                    "description": "匹配行前后附带的上下文行数（默认 0）"
                },
                "encoding": {
                    "type": "string",
                    "required": False,
                    "description": "文件编码（默认 utf-8）"
                }
            },
            "returns": {
                "matches": "匹配结果列表（含 file, line_number, line_content, context_before, context_after）",
                "match_count": "总匹配数",
                "searched_files": "实际搜索的文件数量"
            },
            "examples": [
                {
                    "path": "src/main.py",
                    "pattern": "def main",
                    "description": "在 main.py 中查找函数定义"
                },
                {
                    "path": "src",
                    "pattern": "^class .*",
                    "is_regex": True,
                    "recursive": True,
                    "context_lines": 2,
                    "description": "递归搜索 src 下所有类定义，并显示前后 2 行"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
