"""SubAgentSoulV6InfoGathererContextHandler — 信息收集子 Agent V6 上下文处理器

在 SubAgentSoulV6ContextHandler 基础上：
  - 使用 InfoGatherer 专属系统提示词
  - ALLOWED_TOOLS 仅暴露读写/下载/网络工具
  - 移除旧的 Layer 1+2 截断/压缩逻辑（由 V6 落盘+驱逐替代）
  - 支持通过配置 ``info_gatherer.compress.tool_output_max_tokens`` 覆盖落盘阈值
"""

from pathlib import Path
from typing import Any

from ...llm import UnifiedMessage
from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2
from ..system_prompts.subagent_info_gatherer import INFO_GATHERER_SYSTEM_PROMPT
from .subagent_soulv6_context import SubAgentSoulV6ContextHandler
from .subagent_soulv6_tool_result_read_tool import TOOL_NAME as _READ_TOOL_NAME

_rlog = get_running_log()


class SubAgentSoulV6InfoGathererContextHandler(SubAgentSoulV6ContextHandler):
    """信息收集子 Agent V6 上下文处理器"""

    ALLOWED_TOOLS = frozenset({
        "download_file",
        "file_edit",
        "file_info",
        "file_line",
        "glob",
        "list_directory",
        "mkdir",
        "read_file",
        "read_image_base64",
        "search_text",
        "web_fetch",
        "web_search",
        "write_file",
        # V6 内联工具
        _READ_TOOL_NAME,
    })

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        memory_mode: str = "empty",
        initial_messages: list[UnifiedMessage] | None = None,
        initial_system: str | None = None,
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        # 用旧配置键覆盖 V6 落盘阈值（向后兼容）
        strategy_cfg: dict[str, Any] = (
            config.get("context", {}) if isinstance(config, dict) else {}
        )
        compress_cfg: dict[str, Any] = strategy_cfg.get("info_gatherer", {}).get(
            "compress", {}
        )
        spill_override = compress_cfg.get("tool_output_max_tokens")

        if initial_system is None:
            initial_system = INFO_GATHERER_SYSTEM_PROMPT.format(
                workspace_path=str(workspace_path)
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

        # 向后兼容：用旧截断配置覆盖落盘阈值
        if spill_override is not None:
            self._artifact_store.spill_token_threshold = int(spill_override)

        _rlog.info(
            f"session_{session_id}",
            f"[SubAgentSoulV6InfoGathererContextHandler] 初始化 "
            f"(spill_threshold={self._artifact_store.spill_token_threshold}, "
            f"allowed_tools={sorted(self.ALLOWED_TOOLS)}, "
            f"workspace={workspace_path})",
        )

    # ------------------------------------------------------------------
    # 工具列表：V6 基类工具过滤到 ALLOWED_TOOLS
    # ------------------------------------------------------------------

    def _build_tool_list(self):
        from ...llm.types import tool_description_to_definition
        from ...tool.tool_manager import ToolManager

        tool_manager = ToolManager.get_instance()
        all_tools_info = tool_manager.get_all_tools_info()

        tool_defs = []
        if all_tools_info:
            for tool_name in all_tools_info:
                if tool_name not in self.ALLOWED_TOOLS:
                    continue
                tool_instance = tool_manager.get_tool(tool_name)
                if not tool_instance.is_enabled():
                    continue
                desc = tool_instance.get_description()
                if not isinstance(desc, dict):
                    continue
                td = tool_description_to_definition(desc)
                if td is not None:
                    tool_defs.append(td)

        # 追加 V6 内联 read tool
        tool_defs.append(self._read_tool.get_tool_definition())

        return tool_defs if tool_defs else None
