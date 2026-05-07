"""GlobTool - 基于 glob 模式匹配本地文件"""

from pathlib import Path
from typing import Any

from ...logging import get_running_log
from ..base_tool import ToolResult, ToolStatus
from .fs_base_tool import FileSystemBaseTool

_rlog = get_running_log()


class GlobTool(FileSystemBaseTool):
    """文件模式匹配工具

    功能：
    - 使用 glob 模式搜索本地文件/目录
    - 支持递归搜索（**/*.py 等）
    - 返回相对于搜索基准目录的路径列表
    - 可限制最大返回数量

    配置示例：
    ```yaml
    glob:
      tool_type: "glob"
      tool_name: "glob"
      enabled: true
      working_dir: null
      allowed_paths: []
      blocked_paths: []
    ```
    """

    # 仅读取目录/文件结构，不修改任何状态
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list]:
        b = args.get("base_dir")
        return {"read": [b] if isinstance(b, str) and b.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行 glob 文件匹配

        Args:
            pattern: glob 模式，如 **/*.py、src/*.txt
            base_dir: 搜索基准目录（默认 working_dir）
            max_results: 最大返回数量（默认 1000）

        Returns:
            ToolResult: 包含 matches, count, truncated
        """
        valid, error_msg = self.validate_params(["pattern"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        pattern = kwargs["pattern"]
        max_results = max(1, self._coerce_int(kwargs.get("max_results", 1000), default=1000))

        # 解析基准目录
        base_dir_raw = kwargs.get("base_dir")
        if base_dir_raw:
            base = self._resolve_path(base_dir_raw)
        else:
            base = Path(self.working_dir).resolve()

        # 安全检查：基准目录必须存在且为目录
        ok, err = self._security_check(base, require_exists=True, must_be_dir=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        try:
            _rlog.info("core_service", f"glob 搜索: base={base}, pattern={pattern}, max_results={max_results}")

            raw_matches: list[Path] = sorted(base.glob(pattern))

            truncated = len(raw_matches) > max_results
            matches_slice = raw_matches[:max_results]

            # 转为相对路径字符串
            matches: list[str] = []
            for m in matches_slice:
                try:
                    matches.append(str(m.relative_to(base)))
                except ValueError:
                    matches.append(str(m))

            _rlog.info("core_service",
                f"glob 完成: 匹配 {len(raw_matches)} 个，返回 {len(matches)} 个, truncated={truncated}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "matches": matches,
                    "count": len(matches),
                    "truncated": truncated,
                },
                metadata={
                    "base_dir": str(base),
                    "pattern": pattern,
                    "max_results": max_results,
                }
            )

        except Exception as e:
            error_msg = f"glob 搜索异常: pattern={pattern}, base={base}, 错误: {e!s}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """对 matches 列表中的每个路径字符串进行脱敏。"""
        if not isinstance(raw_output, dict):
            return raw_output
        masked = dict(raw_output)
        if "matches" in masked and isinstance(masked["matches"], list):
            masked["matches"] = [
                self._mask_string(m, path_mask_map) if isinstance(m, str) else m
                for m in masked["matches"]
            ]
        return masked

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "使用 glob 模式匹配本地文件/目录，返回相对路径列表",
            "parameters": {
                "pattern": {
                    "type": "string",
                    "required": True,
                    "description": "glob 模式，如 **/*.py、src/*.txt、*.{json,yaml}"
                },
                "base_dir": {
                    "type": "string",
                    "required": False,
                    "description": "搜索基准目录（默认 working_dir）"
                },
                "max_results": {
                    "type": "integer",
                    "required": False,
                    "description": "最大返回数量（默认 1000）"
                }
            },
            "returns": {
                "matches": "匹配的文件/目录路径列表（相对于 base_dir）",
                "count": "实际返回的匹配数量",
                "truncated": "结果是否被截断（超出 max_results）"
            },
            "examples": [
                {
                    "pattern": "**/*.py",
                    "description": "递归查找所有 Python 文件"
                },
                {
                    "pattern": "src/*.txt",
                    "base_dir": "project",
                    "max_results": 50,
                    "description": "在 project/src 下查找最多 50 个 txt 文件"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
