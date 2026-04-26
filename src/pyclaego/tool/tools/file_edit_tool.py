"""FileEdit 工具 - 通过字符串替换精确修改文件内容"""

from pathlib import Path
from typing import Dict, Any

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class FileEditTool(FileSystemBaseTool):
    """文件编辑工具（字符串替换）

    功能：
    - 在文件中精确替换指定字符串，无需重写整个文件
    - 默认要求 old_str 在文件中唯一（count=1）；若找到多处匹配则返回匹配行号，
      引导 Agent 提供更多上下文使其唯一
    - 支持 count=0 替换所有匹配

    配置示例：
    ```yaml
    file_edit:
      tool_type: "file_edit"
      tool_name: "file_edit"
      enabled: true
      working_dir: null
      max_file_size: 10485760
      allowed_paths: []
      blocked_paths: []
    ```
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path")
        # file_edit reads then rewrites the same file; declare both.
        v = [p] if isinstance(p, str) and p.strip() else []
        return {"read": v, "write": v}

    async def execute(self, **kwargs) -> ToolResult:
        """执行字符串替换

        Args:
            path:     目标文件路径
            old_str:  要被替换的原始字符串（必须与文件内容完全匹配，含空格和换行）
            new_str:  替换后的新字符串
            encoding: 文件编码（默认 utf-8）
            count:    替换次数（默认 1 表示只替换一处且要求唯一；0 表示替换全部）

        Returns:
            ToolResult: 包含 replaced_count, path, preview_before, preview_after
        """
        valid, error_msg = self.validate_params(["path", "old_str", "new_str"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        old_str: str = kwargs["old_str"]
        new_str: str = kwargs["new_str"]
        encoding: str = kwargs.get("encoding", "utf-8")
        count: int = self._coerce_int(kwargs.get("count", 1), default=1)

        # 安全检查
        ok, err = self._security_check(path, require_exists=True, must_be_file=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        ok, err = self._check_file_size(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        try:
            with open(path, "r", encoding=encoding, errors="replace") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(status=ToolStatus.FAILED, error=f"读取文件失败: {e}")

        # 检查 old_str 是否存在
        occurrence_count = content.count(old_str)
        if occurrence_count == 0:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=(
                    f"未找到要替换的内容。请检查 old_str 是否与文件内容完全匹配"
                    f"（含空格、缩进和换行符）。"
                )
            )

        # count=1 时要求唯一匹配
        if count == 1 and occurrence_count > 1:
            # 找出所有匹配的行号
            lines = content.splitlines(keepends=True)
            match_lines = []
            pos = 0
            line_no = 1
            for line in lines:
                if old_str in (content[pos:pos + len(line)]):
                    match_lines.append(line_no)
                pos += len(line)
                line_no += 1

            # 简化：直接扫描每行
            match_lines = []
            for i, line in enumerate(content.splitlines(), start=1):
                if old_str.splitlines()[0] in line:
                    match_lines.append(i)

            return ToolResult(
                status=ToolStatus.FAILED,
                error=(
                    f"找到 {occurrence_count} 处匹配（出现在行: {match_lines[:10]}），"
                    f"count=1 时要求唯一匹配。请在 old_str 中添加更多上下文使其唯一，"
                    f"或设置 count=0 替换全部匹配。"
                )
            )

        # 执行替换
        replace_count = None if count == 0 else count
        new_content = content.replace(old_str, new_str, replace_count if replace_count else -1)
        actual_replaced = occurrence_count if count == 0 else min(count, occurrence_count)

        try:
            with open(path, "w", encoding=encoding) as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult(status=ToolStatus.FAILED, error=f"写入文件失败: {e}")

        preview_before = old_str[:200] + ("..." if len(old_str) > 200 else "")
        preview_after = new_str[:200] + ("..." if len(new_str) > 200 else "")

        _rlog.info(
            "core_service",
            f"file_edit: {path} | replaced={actual_replaced} | old={preview_before!r}"
        )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={
                "replaced_count": actual_replaced,
                "path": str(path),
                "preview_before": preview_before,
                "preview_after": preview_after,
            },
            metadata={"path": str(path), "encoding": encoding},
        )

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "通过字符串替换精确修改文件内容。"
                "old_str 必须与文件中的内容完全匹配（含空格、缩进、换行符）。"
                "默认 count=1 要求 old_str 在文件中唯一；若有多处匹配会返回行号，"
                "请在 old_str 中补充上下文使其唯一。设置 count=0 可替换所有匹配。"
                "建议先用 read_file 查看内容，再执行替换。"
            ),
            "parameters": {
                "path":     {"type": "string",  "required": True,  "description": "目标文件路径"},
                "old_str":  {"type": "string",  "required": True,  "description": "要被替换的原始字符串（须与文件内容完全一致）"},
                "new_str":  {"type": "string",  "required": True,  "description": "替换后的新字符串"},
                "encoding": {"type": "string",  "required": False, "description": "文件编码（默认 utf-8）"},
                "count":    {"type": "integer", "required": False, "description": "替换次数（默认 1=唯一替换；0=替换全部）"},
            },
            "returns": {
                "replaced_count": "实际替换次数",
                "path":           "文件绝对路径",
                "preview_before": "被替换内容的预览（前 200 字符）",
                "preview_after":  "替换后内容的预览（前 200 字符）",
            },
        }
