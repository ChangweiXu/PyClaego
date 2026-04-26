"""MkdirTool - 安全地创建本地目录"""

from pathlib import Path
from typing import Dict, Any

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class MkdirTool(FileSystemBaseTool):
    """目录创建工具

    功能：
    - 创建指定目录（支持递归创建父目录）
    - 严格的路径安全校验
    - 可配置目录已存在时的行为

    配置示例：
    ```yaml
    mkdir:
      tool_type: "mkdir"
      tool_name: "mkdir"
      enabled: true
      working_dir: null
      allowed_paths: []
      blocked_paths: []
    ```
    """

    # 创建目录会修改文件系统；并发写同一目录存在竞争风险
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path")
        return {"read": [], "write": [p] if isinstance(p, str) and p.strip() else []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行目录创建

        Args:
            path: 要创建的目录路径（相对路径基于 working_dir 解析）
            parents: 是否递归创建父目录（默认 true）
            exist_ok: 目录已存在时是否忽略错误（默认 true）

        Returns:
            ToolResult: 包含 path, created
        """
        valid, error_msg = self.validate_params(["path"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])
        parents = self._coerce_bool(kwargs.get("parents", True), default=True)
        exist_ok = self._coerce_bool(kwargs.get("exist_ok", True), default=True)

        # 安全检查（目录无需提前存在）
        ok, err = self._security_check(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        # 记录是否为新建（调用前是否已存在）
        already_exists = path.exists()

        try:
            _rlog.info("core_service", f"创建目录: {path} (parents={parents}, exist_ok={exist_ok})")

            path.mkdir(parents=parents, exist_ok=exist_ok)

            created = not already_exists
            _rlog.info("core_service", f"目录操作成功: {path}, created={created}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "path": str(path),
                    "created": created,
                },
                metadata={
                    "path": str(path),
                    "parents": parents,
                    "exist_ok": exist_ok,
                }
            )

        except FileExistsError:
            error_msg = f"目录已存在且 exist_ok=False: {path}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)
        except Exception as e:
            error_msg = f"创建目录异常: {path}, 错误: {str(e)}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "安全地创建本地目录，支持递归创建父目录",
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "要创建的目录路径"
                },
                "parents": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否递归创建父目录（默认 true）"
                },
                "exist_ok": {
                    "type": "boolean",
                    "required": False,
                    "description": "目录已存在时是否忽略错误（默认 true）"
                }
            },
            "returns": {
                "path": "创建的目录绝对路径",
                "created": "是否为新建目录（true）还是已存在（false）"
            },
            "examples": [
                {
                    "path": "output/results",
                    "description": "递归创建 output/results 目录"
                },
                {
                    "path": "logs",
                    "parents": False,
                    "exist_ok": False,
                    "description": "仅在 logs 不存在时创建，否则报错"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
