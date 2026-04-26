"""CodeExplorerContextHandler — 代码探索子 Agent 专属上下文处理器

在 InfoGathererContextHandler 基础上：
- 使用代码探索专属系统提示词（含 project_root 和 workspace_path）
- 工具集限制为只读文件浏览工具 + workspace 内写入工具
- 复用 InfoGatherer 的两层压缩机制（单次输出截断 + 旧轮次压缩）
"""

from pathlib import Path
from typing import Dict, Any, List, Optional

from .info_gatherer_context import InfoGathererContextHandler
from ...task_manager import SessionTaskHandlerV2
from ...logging import get_running_log
from ..system_prompts.subagent_code_explorer import CODE_EXPLORER_SYSTEM_PROMPT

_rlog = get_running_log()


class CodeExplorerContextHandler(InfoGathererContextHandler):
    """代码探索子 Agent 专属上下文处理器

    与 InfoGathererContextHandler 的区别：
    - 使用 CodeExplorer 专属系统提示词（含 project_root）
    - ALLOWED_TOOLS 更精简：仅保留只读文件工具 + workspace 写入
    - 无网络工具、无文件编辑工具
    """

    ALLOWED_TOOLS = frozenset({
        "file_info",
        "find_line",
        "glob",
        "list_directory",
        "mkdir",
        "read_file",
        "search_text",
        "write_file",
    })

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: Dict[str, Any],
        project_root: str = ".",
        memory_mode: str = "empty",
        initial_messages: Optional[List] = None,
        initial_system: Optional[str] = None,
        session_task_handler: Optional[SessionTaskHandlerV2] = None,
    ) -> None:
        # 解析 project_root 为绝对路径
        resolved_project_root = str(Path(project_root).expanduser().resolve())

        # 如果调用方未传入自定义系统提示词，使用 CodeExplorer 专属提示词
        if initial_system is None:
            initial_system = CODE_EXPLORER_SYSTEM_PROMPT.format(
                workspace_path=str(workspace_path),
                project_root=resolved_project_root,
            )

        # 调用 InfoGathererContextHandler.__init__
        # 注意：传入 initial_system 以跳过 InfoGatherer 的默认提示词
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
            f"[CodeExplorerContextHandler] 初始化完成 "
            f"(project_root={resolved_project_root}, "
            f"allowed_tools={sorted(self.ALLOWED_TOOLS)}, "
            f"workspace={workspace_path})",
        )
