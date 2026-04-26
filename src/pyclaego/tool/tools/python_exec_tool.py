"""Python 执行工具 - 执行 Python 文件或代码字符串"""

import asyncio
import sys
import tempfile
import os
from pathlib import Path
import traceback
from typing import Dict, Any, Optional

from ..base_tool import BaseTool, ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class PythonExecTool(BaseTool):
    """Python 代码执行工具

    功能：
    - 执行指定路径的 Python 文件
    - 执行传入的 Python 代码字符串（写入临时文件后执行）
    - 可在配置中指定 Python 解释器路径
    - 支持超时控制
    - 捕获 stdout、stderr 和返回码

    配置示例：
    ```yaml
    python_exec:
      tool_type: "python_exec"
      tool_name: "python_exec"
      enabled: true
      timeout: 60
      python_path: "/Users/name/.venv/bin/python"  # 留空则使用当前解释器
      working_dir: null  # 执行工作目录（null 则使用临时目录或文件所在目录）
    ```
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def __init__(self, tool_config: Dict[str, Any]):
        super().__init__(tool_config)
        # Python 解释器路径，留空则回落到当前进程解释器
        self.python_path: str = tool_config.get("python_path", "") or sys.executable
        self.working_dir: Optional[str] = tool_config.get("working_dir", None)

    async def execute(self, **kwargs) -> ToolResult:
        """执行 Python 文件或代码字符串

        Args:
            file_path (str, 可选): 要执行的 Python 文件的绝对路径。
            code     (str, 可选): 要执行的 Python 代码字符串。
            args     (list[str], 可选): 传递给脚本的命令行参数列表（仅对 file_path 有意义）。
            至少提供 file_path 或 code 其中之一；若两者都提供，优先使用 file_path。

        Returns:
            ToolResult: 包含 stdout、stderr、return_code 的执行结果
        """
        file_path: Optional[str] = kwargs.get("file_path")
        code: Optional[str] = kwargs.get("code")
        args: list = kwargs.get("args") or []

        if not file_path and not code:
            return ToolResult(
                status=ToolStatus.FAILED,
                error="必须提供 file_path 或 code 参数之一",
            )

        tmp_file: Optional[str] = None
        try:
            if file_path:
                target = Path(file_path)
                if not target.exists():
                    return ToolResult(
                        status=ToolStatus.FAILED,
                        error=f"文件不存在: {file_path}",
                    )
                if not target.is_file():
                    return ToolResult(
                        status=ToolStatus.FAILED,
                        error=f"路径不是文件: {file_path}",
                    )
                run_path = str(target.resolve())
                cwd = self.working_dir or str(target.parent)
            else:
                # 将代码写入临时文件
                fd, tmp_file = tempfile.mkstemp(suffix=".py", prefix="pyclaego_exec_")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(code or "")
                run_path = tmp_file
                cwd = self.working_dir or os.path.dirname(tmp_file)

            _rlog.info("core_service", f"[PythonExecTool] 执行: {self.python_path} {run_path}")

            process = await asyncio.create_subprocess_exec(
                self.python_path,
                run_path,
                *[str(a) for a in args],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolResult(
                    status=ToolStatus.TIMEOUT,
                    error=f"执行超时 ({self.timeout}秒)",
                    metadata={"python_path": self.python_path, "run_path": run_path},
                )

            stdout_str = stdout_b.decode("utf-8", errors="replace")
            stderr_str = stderr_b.decode("utf-8", errors="replace")
            return_code = process.returncode
            output = {"stdout": stdout_str, "stderr": stderr_str, "return_code": return_code}
            meta = {"python_path": self.python_path, "run_path": run_path}

            if return_code == 0:
                _rlog.info("core_service", "[PythonExecTool] 执行成功 (return_code=0)")
                return ToolResult(status=ToolStatus.SUCCESS, output=output, metadata=meta)
            else:
                _rlog.warning("core_service", f"[PythonExecTool] 执行失败 (return_code={return_code})")
                error_msg = f"Python 脚本返回非零状态码: {return_code}"
                if stderr_str:
                    error_msg += f"\n{stderr_str}"
                return ToolResult(
                    status=ToolStatus.FAILED,
                    output=output,
                    error=error_msg,
                    metadata=meta,
                )

        except Exception as e:
            _rlog.error("core_service", f"[PythonExecTool] 执行异常: {e}\n{traceback.format_exc()}")
            return ToolResult(status=ToolStatus.FAILED, error=f"执行异常: {str(e)}")
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        return super().mask_output(raw_output, path_mask_map)

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "执行 Python 文件或代码字符串，返回 stdout、stderr 和返回码。"
                "使用配置中指定的 Python 解释器路径运行。"
            ),
            "parameters": {
                "file_path": {
                    "type": "string",
                    "required": False,
                    "description": "要执行的 Python 文件的绝对路径（与 code 二选一，优先于 code）",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "required": False,
                    "description": "传递给脚本的命令行参数列表，等价于 sys.argv[1:]（仅 file_path 模式有意义）",
                },
                "code": {
                    "type": "string",
                    "required": False,
                    "description": "要执行的 Python 代码字符串（与 file_path 二选一）",
                },
            },
            "returns": {
                "stdout": "标准输出",
                "stderr": "标准错误",
                "return_code": "进程返回码",
            },
            "examples": [
                {"file_path": "/path/to/script.py", "description": "执行指定 Python 文件"},
                {"code": "print('hello world')", "description": "执行代码字符串"},
            ],
            "config": {
                "python_path": self.python_path,
                "working_dir": self.working_dir,
                "timeout": self.timeout,
            },
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
