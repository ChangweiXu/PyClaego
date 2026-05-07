"""SubAgentSoulV6CodeExplorerContextHandler — 代码探索子 Agent V6 上下文处理器

在 SubAgentSoulV6InfoGathererContextHandler 基础上：
  - 使用 CodeExplorer 专属系统提示词（含 project_root）
  - ALLOWED_TOOLS 更精简：仅只读文件工具 + workspace 写入，无网络工具
  - 完整继承 V6 落盘 + 驱逐 + 预算 + 技能注入逻辑
"""

from pathlib import Path
from typing import Any

from ...llm import UnifiedMessage
from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2
from ..system_prompts.subagent_code_explorer import CODE_EXPLORER_SYSTEM_PROMPT
from .subagent_soulv6_info_gatherer_context import (
    SubAgentSoulV6InfoGathererContextHandler,
)
from .subagent_soulv6_tool_result_read_tool import TOOL_NAME as _READ_TOOL_NAME

_rlog = get_running_log()


class SubAgentSoulV6CodeExplorerContextHandler(
    SubAgentSoulV6InfoGathererContextHandler
):
    """代码探索子 Agent V6 上下文处理器"""

    ALLOWED_TOOLS = frozenset({
        "file_info",
        "find_line",
        "glob",
        "list_directory",
        "mkdir",
        "read_file",
        "search_text",
        "write_file",
        # V6 内联工具
        _READ_TOOL_NAME,
    })

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        project_root: str = ".",
        memory_mode: str = "empty",
        initial_messages: list[UnifiedMessage] | None = None,
        initial_system: str | None = None,
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        resolved_project_root = str(Path(project_root).expanduser().resolve())

        if initial_system is None:
            initial_system = CODE_EXPLORER_SYSTEM_PROMPT.format(
                workspace_path=str(workspace_path),
                project_root=resolved_project_root,
            )

        super().__init__(
            session_id=session_id,
            workspace_path=workspace_path,
            config=config,
            memory_mode=memory_mode,
            initial_messages=initial_messages,
            initial_system=initial_system,
            session_task_handler=session_task_handler,
        )

        self._project_root = resolved_project_root

        _rlog.info(
            f"session_{session_id}",
            f"[SubAgentSoulV6CodeExplorerContextHandler] 初始化 "
            f"(project_root={resolved_project_root}, "
            f"allowed_tools={sorted(self.ALLOWED_TOOLS)}, "
            f"workspace={workspace_path})",
        )
