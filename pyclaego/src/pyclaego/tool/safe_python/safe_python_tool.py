"""SafePythonTool — AST 安全验证的 Python 代码执行工具

与 PythonExecTool 的核心区别：
  - 代码经过 AST 级解析 → 结构验证 → 安全策略验证后才执行
  - 在独立子进程（spawn）中执行，提供进程级隔离与真正的超时保证
  - __builtins__ 被限制为白名单集合（无 eval/exec/open 等危险内置）
  - 所有 import 必须在策略注册表中预先注册
  - 禁止所有 dunder 属性访问（防止沙盒逃逸）
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from ..base_tool import BaseTool, ToolResult, ToolStatus
from .exceptions import SafePythonError, SandboxTimeoutError
from .executor import ExecResult, SandboxExecutor
from .policy import REGISTRY
from .validator import ASTValidator

_rlog = get_running_log()


class SafePythonTool(BaseTool):
    """AST 安全验证的 Python 代码执行工具。

    执行流程（四阶段）：
      1. ast.parse()     — 语法解析，SyntaxError 立即返回 FAILED
      2. ASTValidator    — 结构检查（嵌套深度 / 语句数 / 行数）
      3. REGISTRY.validate_ast() — 安全策略检查（模块白名单 / 禁止名称 / dunder 访问）
      4. SandboxExecutor — 在 spawn 子进程中执行，受限 __builtins__ + 受限 __import__

    配置示例（tools.yaml）：
    ```yaml
    safe_python:
      tool_type: "safe_python"
      tool_name: "safe_python"
      enabled: true
      timeout: 30           # 执行超时（秒），超时后子进程被强杀
      max_memory_mb: 256    # 子进程内存上限（MiB，Unix 有效）
      max_depth: 8          # 代码最大嵌套深度
      max_stmts: 200        # 代码最大语句数
      max_lines: 500        # 代码最大行数
      path_scope: null      # 保留字段：未来版本将限制文件访问路径
      working_dir: null     # 保留字段
    ```
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def __init__(self, tool_config: dict[str, Any]):
        super().__init__(tool_config)
        self.max_memory_mb: int = int(tool_config.get("max_memory_mb", 256))
        self.max_depth: int = int(tool_config.get("max_depth", ASTValidator.DEFAULT_MAX_DEPTH))
        self.max_stmts: int = int(tool_config.get("max_stmts", ASTValidator.DEFAULT_MAX_STMTS))
        self.max_lines: int = int(tool_config.get("max_lines", ASTValidator.DEFAULT_MAX_LINES))
        self.path_scope: str | None = tool_config.get("path_scope") or None  # 保留字段
        self.working_dir: str | None = tool_config.get("working_dir") or None  # 保留字段
        self._executor = SandboxExecutor(
            timeout=self.timeout,
            max_memory_mb=self.max_memory_mb,
        )

    # ------------------------------------------------------------------
    # BaseTool 抽象方法实现
    # ------------------------------------------------------------------

    async def execute(self, **kwargs) -> ToolResult:
        """执行 Python 代码（经 AST 安全验证后在沙盒中运行）。

        Args (via kwargs):
            code      (str, 可选): 要执行的 Python 代码字符串
            file_path (str, 可选): 要执行的 Python 文件路径（绝对路径）
            至少提供 code 或 file_path 其中之一；若两者都提供，优先使用 file_path。

        Returns:
            ToolResult，output 为 {stdout, stderr, exec_time_ms}
        """
        code: str | None = kwargs.get("code")
        file_path: str | None = kwargs.get("file_path")

        if not code and not file_path:
            return ToolResult(
                status=ToolStatus.FAILED,
                error="必须提供 code 或 file_path 参数之一",
            )

        # 1. 读取源码
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
            try:
                source = target.read_text(encoding="utf-8")
            except OSError as e:
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error=f"读取文件失败: {e}",
                )
        else:
            source = code  # type: ignore[assignment]

        approved_modules: set[str] = set()

        try:
            # 2. 语法解析
            _rlog.info("core_service", "[SafePythonTool] 阶段 1/4：AST 解析")
            try:
                tree = ast.parse(source)
            except SyntaxError as e:
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error=f"语法错误: {e}",
                    metadata={"stage": "parse"},
                )

            # 3. 结构验证
            _rlog.info("core_service", "[SafePythonTool] 阶段 2/4：结构验证")
            ASTValidator.validate(
                tree,
                max_depth=self.max_depth,
                max_stmts=self.max_stmts,
                max_lines=self.max_lines,
            )

            # 4. 安全策略验证（收集授权模块）
            _rlog.info("core_service", "[SafePythonTool] 阶段 3/4：安全策略验证")
            approved_modules = REGISTRY.validate_ast(tree)

            # 5. 沙盒执行
            _rlog.info(
                "core_service",
                f"[SafePythonTool] 阶段 4/4：沙盒执行（授权模块: {sorted(approved_modules)}）",
            )
            result: ExecResult = await self._executor.execute(source, approved_modules)

        except SandboxTimeoutError as e:
            _rlog.warning("core_service", f"[SafePythonTool] 执行超时: {e}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=str(e),
                metadata={"stage": "execute", "timeout": True},
            )
        except SafePythonError as e:
            _rlog.warning("core_service", f"[SafePythonTool] 安全拦截: {e}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=str(e),
                metadata={"stage": "validate"},
            )
        except Exception as e:
            _rlog.error("core_service", f"[SafePythonTool] 意外异常: {e}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"执行异常: {e}",
                metadata={"stage": "unknown"},
            )

        # 6. 构建返回结果
        status = ToolStatus.SUCCESS if result.success else ToolStatus.FAILED
        if not result.success:
            _rlog.warning(
                "core_service",
                f"[SafePythonTool] 代码运行时错误: {result.error}",
            )
        else:
            _rlog.info(
                "core_service",
                f"[SafePythonTool] 执行成功，耗时 {result.exec_time_ms}ms",
            )

        return ToolResult(
            status=status,
            output={
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exec_time_ms": result.exec_time_ms,
            },
            error=result.error,
            metadata={
                "approved_modules": sorted(approved_modules),
                "exec_time_ms": result.exec_time_ms,
            },
        )

    def get_description(self) -> dict[str, Any]:
        registered_modules = REGISTRY.list_modules()
        return {
            "name": self.tool_name,
            "description": (
                "在 AST 安全验证的沙盒中执行 Python 代码。\n"
                "代码经过以下四个阶段处理后，在独立子进程中执行：\n"
                "  1. AST 语法解析\n"
                "  2. 结构验证（嵌套深度、语句数、行数）\n"
                "  3. 安全策略验证（模块白名单、禁止 eval/exec/open 等、禁止 dunder 访问）\n"
                "  4. 受限沙盒执行（__builtins__ 为白名单，模块导入受限于已注册模块）\n\n"
                f"已注册的可用模块: {registered_modules}\n\n"
                "禁止的操作（会在验证阶段被拒绝）：\n"
                "  - eval() / exec() / compile()\n"
                "  - open() / os / sys / subprocess 等系统访问\n"
                "  - 所有 dunder 属性访问（如 __class__、__globals__、__dict__）\n"
                "  - 相对导入 / 通配符导入 (from x import *)\n"
                "  - 未注册模块的 import\n\n"
                "注意：\n"
                "  - 类定义（def __init__ 等方法定义）是允许的，"
                "只有属性访问（obj.__init__）被拒绝\n"
                "  - from __future__ import ... 始终被允许\n"
                "  - numpy / pandas 的文件 I/O 函数部分被屏蔽，详见策略注释"
            ),
            "parameters": {
                "code": {
                    "type": "string",
                    "required": False,
                    "description": (
                        "要执行的 Python 代码字符串。\n"
                        "code 和 file_path 至少提供一个；若两者都提供，优先使用 file_path。"
                    ),
                },
                "file_path": {
                    "type": "string",
                    "required": False,
                    "description": "要执行的 Python 文件的绝对路径。",
                },
            },
            "returns": {
                "stdout": "标准输出（字符串）",
                "stderr": "标准错误（字符串）",
                "exec_time_ms": "执行耗时（毫秒）",
            },
            "security": {
                "registered_modules": registered_modules,
                "blocked_builtins": sorted([
                    "eval", "exec", "compile", "open", "input",
                    "globals", "locals", "vars", "breakpoint",
                ]),
                "dunder_attribute_access_blocked": True,
                "relative_import_blocked": True,
                "wildcard_import_blocked": True,
                "process_isolation": True,
                "timeout_s": self.timeout,
                "max_memory_mb": self.max_memory_mb,
            },
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """对 stdout/stderr 中的真实路径进行脱敏。"""
        if not isinstance(raw_output, dict):
            return raw_output
        masked = dict(raw_output)
        if "stdout" in masked:
            masked["stdout"] = self._mask_string(masked["stdout"], path_mask_map)
        if "stderr" in masked:
            masked["stderr"] = self._mask_string(masked["stderr"], path_mask_map)
        return masked
