"""SafeBashTool — 结构化命令树执行工具

LLM 通过 command_tree 参数传入 XML 或 JSON 格式的命令树，
本工具负责：解析 → 结构验证 → 注册表/安全验证 → 执行。

任何验证失败都以 ToolResult(FAILED) 返回，不抛出异常给调用方。
"""

from __future__ import annotations

from typing import Any

from ...logging import get_running_log
from ..base_tool import BaseTool, ToolResult, ToolStatus
from .exceptions import SafeBashError
from .executor import ExecResult, TreeExecutor
from .parser import parse
from .registry import REGISTRY
from .tree import TreeValidator

_rlog = get_running_log()

# 工具描述中内嵌的 schema 示例（让 LLM 明确知道格式）
_XML_EXAMPLE = """\
<bash cwd="/tmp">
  <pipeline>
    <cmd name="ls"><arg>-lha</arg><arg>~/Documents</arg></cmd>
    <cmd name="grep"><arg>-E</arg><arg>\\.py$</arg></cmd>
  </pipeline>
</bash>"""

_JSON_EXAMPLE = """\
{
  "op": "pipeline",
  "cwd": "/tmp",
  "steps": [
    {"op": "cmd", "name": "ls",   "args": ["-lha", "~/Documents"]},
    {"op": "cmd", "name": "grep", "args": ["-E", "\\.py$"]}
  ]
}"""

_AND_XML_EXAMPLE = """\
<bash>
  <and>
    <cmd name="test"><arg>-d</arg><arg>/tmp/work</arg></cmd>
    <cmd name="echo"><arg>dir exists</arg></cmd>
  </and>
</bash>"""


class SafeBashTool(BaseTool):
    """结构化安全 Bash 工具

    与 BashTool 的核心区别：
    - LLM 输入为 XML/JSON 命令树（非原始 bash 字符串）
    - 命令树经过 AST 级解析、结构验证、注册表安全验证后才执行
    - 执行使用 subprocess_exec（无 shell 中间层，消除注入风险）
    - 不支持变量展开（$VAR），只展开 ~ 前缀

    配置示例（tools.yaml）：
    ```yaml
    safe_bash:
      tool_type: "safe_bash"
      tool_name: "safe_bash"
      enabled: true
      timeout: 30
      input_format: "auto"        # "xml" | "json" | "auto"（默认）
      path_scope: null            # 可选：限制路径参数范围，如 "/home/agent"
      working_dir: null           # 可选：默认工作目录
    ```
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    def __init__(self, tool_config: dict[str, Any]):
        super().__init__(tool_config)
        self.input_format: str = tool_config.get("input_format", "auto")
        self.path_scope: str | None = tool_config.get("path_scope") or None
        self.working_dir: str | None = tool_config.get("working_dir") or None
        self._executor = TreeExecutor(timeout=self.timeout)

    # ------------------------------------------------------------------
    # BaseTool 抽象方法实现
    # ------------------------------------------------------------------

    def extract_paths(self, args: dict[str, Any]) -> dict[str, Any]:
        """Parse the supplied command tree and return the read/write paths.

        Returns ``{"read": [], "write": []}`` if the command tree is
        absent or fails to parse — the actual ``execute`` call will
        report a structured error in that case.
        """
        raw = args.get("command_tree")
        if not isinstance(raw, str) or not raw.strip():
            return {"read": [], "write": []}
        try:
            tree = parse(raw, fmt=self.input_format)
        except Exception:
            return {"read": [], "write": []}
        return REGISTRY.extract_paths_from_tree(tree)

    async def execute(self, **kwargs) -> ToolResult:
        """执行命令树。

        Args (via kwargs):
            command_tree (str): XML 或 JSON 格式的命令树字符串

        Returns:
            ToolResult，output 为 {stdout, stderr, return_code}
        """
        # 1. 参数校验
        valid, error_msg = self.validate_params(["command_tree"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        raw_input: str = kwargs["command_tree"]

        try:
            # 2. 解析
            _rlog.info("core_service", f"[SafeBashTool] 解析命令树 (fmt={self.input_format})")
            tree = parse(raw_input, fmt=self.input_format)

            # 3. 结构验证
            TreeValidator.validate(tree)

            # 4. 注册表 + 安全验证
            REGISTRY.validate_tree(tree, path_scope=self.path_scope)

            # 5. 执行
            _rlog.info("core_service", "[SafeBashTool] 开始执行命令树")
            result: ExecResult = await self._executor.execute(tree, cwd=self.working_dir)

        except SafeBashError as e:
            _rlog.warning("core_service", f"[SafeBashTool] 安全拦截: {e}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=str(e),
                metadata={"command_tree": raw_input, "working_dir": self.working_dir},
            )
        except Exception as e:
            _rlog.error("core_service", f"[SafeBashTool] 意外异常: {e}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"执行异常: {e}",
                metadata={"command_tree": raw_input, "working_dir": self.working_dir},
            )

        # 6. 构建返回结果
        status = ToolStatus.SUCCESS if result.success else ToolStatus.FAILED
        if not result.success:
            _rlog.warning(
                "core_service",
                f"[SafeBashTool] 命令返回非零状态码: {result.returncode}",
            )
        else:
            _rlog.info("core_service", f"[SafeBashTool] 执行成功，rc={result.returncode}")

        return ToolResult(
            status=status,
            output={
                "stdout": result.stdout_str,
                "stderr": result.stderr_str,
                "return_code": result.returncode,
            },
            error=f"命令返回非零状态码: {result.returncode}" if not result.success else None,
            metadata={"command_tree": raw_input, "working_dir": self.working_dir},
        )

    def get_description(self) -> dict[str, Any]:
        registered_cmds = REGISTRY.list_commands()
        return {
            "name": self.tool_name,
            "description": (
                "以结构化 XML 或 JSON 命令树的方式执行 Shell 命令。\n"
                "命令树经过解析、结构验证、安全注册表验证后，"
                "使用 subprocess_exec 执行（无 shell 中间层，防止命令注入）。\n"
                "不支持 $VAR 环境变量展开；~ 路径前缀会被自动展开。\n\n"
                f"已注册的可用命令: {registered_cmds}\n\n"
                "支持的结构节点: cmd, pipeline (|), seq (;), and (&&), or (||)\n\n"
                f"XML 格式示例（管道）:\n{_XML_EXAMPLE}\n\n"
                f"JSON 格式示例（管道）:\n{_JSON_EXAMPLE}\n\n"
                f"XML 格式示例（条件执行 &&）:\n{_AND_XML_EXAMPLE}"
            ),
            "parameters": {
                "command_tree": {
                    "type": "string",
                    "required": True,
                    "description": (
                        "XML 或 JSON 格式的命令树字符串。\n"
                        "XML：根标签为 <bash>，子节点为 <cmd>/<pipeline>/<seq>/<and>/<or>。\n"
                        "JSON：根对象含 'op' 字段，值为 'cmd'/'pipeline'/'seq'/'and'/'or'。\n"
                        "cmd 节点必须使用注册表中的合法命令名。"
                    ),
                }
            },
            "returns": {
                "stdout": "标准输出（字符串）",
                "stderr": "标准错误（字符串）",
                "return_code": "命令返回码（整数）",
            },
            "examples": [
                {
                    "description": "列出 ~/Documents 中的 .py 文件（XML，管道）",
                    "command_tree": _XML_EXAMPLE,
                },
                {
                    "description": "列出 ~/Documents 中的 .py 文件（JSON，管道）",
                    "command_tree": _JSON_EXAMPLE,
                },
                {
                    "description": "检查目录存在后打印提示（XML，&&）",
                    "command_tree": _AND_XML_EXAMPLE,
                },
            ],
            "security": {
                "registered_commands": registered_cmds,
                "path_scope": self.path_scope or "不限制",
                "no_shell": True,
                "no_variable_expansion": True,
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
