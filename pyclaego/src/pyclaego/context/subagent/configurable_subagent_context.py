"""ConfigurableSubAgentContext — 配置驱动的通用子代理上下文处理器

替代现有的 BaseSubAgentContextHandler → SubAgentSummarizingContextHandler
→ InfoGathererContextHandler → CodeExplorerContextHandler 四层继承链。

所有差异化行为通过 ToolAgentConfig 参数控制：
  - system_prompt     : 从 profile 注入（支持 {workspace_path} {project_root} 模板）
  - allowed_tools     : _build_tool_list() 按白名单从 ToolManager 动态过滤
  - context_strategy  : 控制压缩/驱逐特性是否启用
  - max_tool_rounds   : 传递给 token 预算页脚
  - skills            : 注入技能提示词到 system_prompt
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...llm import (
    ToolDefinition,
    UnifiedMessage,
    tool_description_to_definition,
)
from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2
from ...tool_agent import ToolAgentConfig
from .subagent_summarizing_context import SubAgentSummarizingContextHandler

_rlog = get_running_log()


class ConfigurableSubAgentContext(SubAgentSummarizingContextHandler):
    """配置驱动的通用子代理上下文处理器。

    继承 SubAgentSummarizingContextHandler（含工具结果落盘、冻结压缩、驱逐工具），
    但通过 ToolAgentConfig 参数化所有差异点：
      - 系统提示词 → profile.system_prompt
      - 工具白名单 → profile.allowed_tools
      - 压缩策略   → 由 profile.context_strategy 控制启用/禁用
      - 技能注入   → profile.skills

    构造参数:
        session_id / workspace_path / config: 同基类
        profile:           ToolAgentConfig（核心驱动参数）
        memory_mode:       "empty" | "inherit"
        initial_messages:  inherit 模式下的初始消息
        initial_system:    覆盖 profile.system_prompt（通常不传）
        project_root:      代码探索目标目录（仅 code_explorer 等需要）
        session_task_handler: TaskHandler 实例
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: dict[str, Any],
        profile: ToolAgentConfig,
        memory_mode: str = "empty",
        initial_messages: list[UnifiedMessage] | None = None,
        initial_system: str | None = None,
        project_root: str = ".",
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> None:
        self._profile = profile

        # 模板变量
        resolved_project_root = str(Path(project_root).expanduser().resolve())

        # 渲染系统提示词（优先级：initial_system > profile.system_prompt）
        if initial_system is None:
            try:
                # 支持两种模板变量名
                rendered_system = profile.render_system_prompt(
                    workspace_path=str(workspace_path),
                    project_root=resolved_project_root,
                )
            except KeyError:
                # 如果模板不含 {project_root}，只传 workspace_path
                rendered_system = profile.render_system_prompt(
                    workspace_path=str(workspace_path),
                )
        else:
            rendered_system = initial_system

        # ── 注入 summarizing_subagent 配置到正确层级 ─────────────────
        # BaseContextHandler.__init__ 做了 self.config = config["context"]，
        # SubAgentSummarizingContextHandler.__init__ 通过
        #   self.config.get("summarizing_subagent", {}) 读取，
        # 因此必须写入 config["context"]["summarizing_subagent"]，而非顶层。
        config = dict(config)
        ctx = dict(config.get("context") or {})
        summarizing = dict(ctx.get("summarizing_subagent") or {})

        # 策略 "none"：禁用压缩特性（冻结循环 + 工具结果落盘）
        if profile.context_strategy == "none":
            summarizing.update({
                "context_pressure_threshold": 2.0,   # 永不触发压缩
                "tool_result_warn_tokens": 999_999,
                "tool_result_truncate_tokens": 999_999,
            })

        # profile.llm 已由 DynamicSpawnSubagentTool 通过 resolve_profile
        # 解析完毕（必非空，最低兜底 "kimi_code"），注入为 compress_llm，
        # 确保冻结循环使用与子代理相同的模型，避免思维链不兼容
        if profile.llm:
            summarizing["compress_llm"] = profile.llm

        ctx["summarizing_subagent"] = summarizing
        config["context"] = ctx

        super().__init__(
            session_id=session_id,
            workspace_path=workspace_path,
            config=config,
            memory_mode=memory_mode,
            initial_messages=initial_messages,
            initial_system=rendered_system,
            session_task_handler=session_task_handler,
        )

        # ── 注入技能到 system prompt ─────────────────────────────────
        # 在 super().__init__() 之后执行，因为 self.session_id / self._subagent_system
        # 均由父类 BaseSubAgentContextHandler 设置。
        self._resolved_skills: list[str] = []
        skills_text = self._resolve_and_format_skills()
        if skills_text:
            self._subagent_system = self._subagent_system + "\n\n---\n\n" + skills_text

        self._project_root = resolved_project_root

        _rlog.info(
            f"session_{session_id}",
            f"[ConfigurableSubAgentContext] 初始化完成 "
            f"(profile={profile.name}, strategy={profile.context_strategy}, "
            f"allowed_tools={sorted(profile.allowed_tools)}, "
            f"skills={len(self._resolved_skills)}, "
            f"llm={profile.llm or '(inherit)'}, "
            f"workspace={workspace_path})",
        )

    # ------------------------------------------------------------------
    # _build_tool_list — 核心：按白名单过滤
    # ------------------------------------------------------------------

    def _build_tool_list(self) -> list[ToolDefinition]:
        """从 ToolManager 加载全部工具，按 profile.allowed_tools 过滤。

        - 空列表 → 返回空列表（echo 模式）
        - ["*"] → 返回所有工具
        - 非空列表 → 仅返回白名单中的工具
        - 遇到单个工具转换失败时跳过并记录警告
        """
        from ...tool import get_tool_manager

        allowed = self._profile.allowed_tools
        if not allowed:
            return []

        uses_all = self._profile.uses_all_tools
        tool_manager = get_tool_manager()
        tool_defs: list[ToolDefinition] = []

        if uses_all:
            # 返回所有已注册工具
            all_names = tool_manager.list_available_tools()
            for tool_name in sorted(all_names):
                tool_inst = tool_manager.get_tool(tool_name)
                if tool_inst is None:
                    continue
                try:
                    desc = tool_inst.get_description()
                    tool_defs.append(tool_description_to_definition(desc))
                except Exception:
                    pass
            return tool_defs

        for tool_name in sorted(allowed):
            tool_inst = tool_manager.get_tool(tool_name)
            if tool_inst is None:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[ConfigurableSubAgentContext] 工具 '{tool_name}' "
                    f"在白名单中但未在 ToolManager 注册，跳过",
                )
                continue
            try:
                desc = tool_inst.get_description()
                tool_defs.append(tool_description_to_definition(desc))
            except Exception as exc:
                _rlog.warning(
                    f"session_{self.session_id}",
                    f"[ConfigurableSubAgentContext] 工具 '{tool_name}' "
                    f"转换失败: {exc}，跳过",
                )

        return tool_defs

    # ------------------------------------------------------------------
    # 技能注入
    # ------------------------------------------------------------------

    def _resolve_and_format_skills(self) -> str:
        """按照 profile.skills 白名单解析技能并格式化为 Markdown。

        处理 "*" 通配符展开、空列表跳过、不存在的技能静默跳过。
        格式与 SubAgentSoulV6ContextHandler._get_skills_info() 保持一致。

        Returns:
            格式化的技能列表 Markdown；无可用技能时返回空字符串
        """
        try:
            from ...skill import get_skill_manager
            from ...tool_agent import get_tool_agent_manager

            # ① 展开 "*" → 实际技能名列表
            tam = get_tool_agent_manager()
            resolved = tam.resolve_skills_for_agent(self._profile, self.session_id)
            self._resolved_skills = resolved

            if not resolved:
                return ""

            # ② 获取技能详情
            sm = get_skill_manager()
            sm.reload_session_skills(self.session_id)
            skill_map = sm.get_all_skills(self.session_id)

            # ③ 组装 Markdown
            lines = ["# 可用技能\n"]
            for i, name in enumerate(resolved, 1):
                skill = skill_map.get(name)
                if skill is None:
                    continue
                lines.append(f"### {i}. {skill.name}")
                lines.append(f"- **路径**: `{skill.path}`")
                lines.append(
                    f"- **描述**: {skill.description or '（无描述）'}"
                )
                lines.append("")

            if len(lines) == 1:
                # 仅 header 无实际技能（全部未找到）
                return ""

            lines.append(
                "> 使用技能前，请先读取对应路径下的 `SKILL.md` 获取完整指引。"
            )
            return "\n".join(lines)
        except Exception as exc:
            _rlog.warning(
                f"session_{self.session_id}",
                f"[ConfigurableSubAgentContext] 技能解析失败: {exc}",
            )
            return ""

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def profile(self) -> ToolAgentConfig:
        return self._profile
