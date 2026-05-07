"""UniversalSubAgent — 配置驱动的通用子代理，复用 PipelineAgent 的 LoopPipeline

所有差异化行为通过 ToolAgentConfig 控制：
  - max_tool_rounds → LoopPipeline 的 max_rounds
  - allowed_tools    → ConfigurableSubAgentContext 过滤工具列表
  - system_prompt    → ConfigurableSubAgentContext 注入
  - llm              → 最终 LLM provider（由 resolve_profile 解析）
  - skills           → ConfigurableSubAgentContext 注入技能提示词

继承 PipelineAgent 以复用其完整的 Pipeline + LoopPipeline + RetryPolicy 机制，
同时保留 BaseSubAgent 的遗言写盘（RESULT.md）机制。
"""
from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2
from ...tool_agent import ToolAgentConfig
from ..agent_pipeline.agent import PipelineAgent

_rlog = get_running_log()

_RESULT_FILE = "RESULT.md"


class UniversalSubAgent(PipelineAgent):
    """配置驱动的通用子代理。

    构造参数:
        config:         主 Agent 配置（含 agent / llm 等字段）
        session_id:     父会话 ID
        subagent_id:    子 Agent 唯一标识
        workspace_path: 子 Agent 独立工作目录
        profile:        ToolAgentConfig（核心驱动参数）
    """

    def __init__(
        self,
        config: dict[str, Any],
        session_id: str,
        subagent_id: str,
        workspace_path: Path,
        profile: ToolAgentConfig,
    ) -> None:
        # PipelineAgent.__init__ 需要完整的 widget_config（config 就是 base_config）
        # BaseAgent.__init__ 会做 self.config = config.get("agent", config)
        super().__init__(config, session_id)

        self.subagent_id: str = subagent_id
        self.workspace_path: Path = Path(workspace_path)
        self._profile = profile

        # profile.llm 已由 resolve_profile 解析完毕，直接使用
        self.set_llm_id(profile.llm)

        # 覆盖 max_tool_rounds（PipelineAgent 从 config["pipeline"]["max_tool_rounds"] 读取）
        self.max_tool_rounds = profile.max_tool_rounds

        _rlog.info(
            f"session_{session_id}",
            f"[UniversalSubAgent] 初始化完成 "
            f"(subagent_id={subagent_id}, profile={profile.name}, "
            f"llm={self.get_llm_id()}, max_rounds={profile.max_tool_rounds}, "
            f"tools={len(profile.allowed_tools)}, "
            f"workspace={workspace_path})",
        )

    # ------------------------------------------------------------------
    # 对外接口 — 兼容 SecurityHandler.request_subagent_call
    # ------------------------------------------------------------------

    async def process(
        self,
        user_message: dict[str, Any],
        context_handler: Any,
        subagent_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        """执行子代理主逻辑，返回 RESULT.md 全文。

        内部委托给 process_v2（PipelineAgent 的 Pipeline 执行），
        finally 块负责写遗言（RESULT.md）。
        """
        user_text = (
            user_message.get("content", "")
            if isinstance(user_message, dict)
            else str(user_message)
        )

        await subagent_task_handler.log_info(
            f"[UniversalSubAgent: {self.subagent_id}] 开始执行 "
            f"(profile={self._profile.name}, prompt=`{user_text[:100]}...`)"
        )

        final_response: str = ""
        exc_caught: BaseException | None = None

        try:
            # 委托给 PipelineAgent.process_v2 走完整 Pipeline
            final_response = await self.process_v2(
                user_message=user_message,
                context_handler=context_handler,
                session_task_handler=subagent_task_handler,
                **kwargs,
            )
        except Exception as exc:
            exc_caught = exc
            _rlog.error(
                f"session_{self.session_id}",
                f"[UniversalSubAgent: {self.subagent_id}] 执行异常: {exc}\n"
                f"{traceback.format_exc()}",
            )
        finally:
            # 写遗言
            self._write_result(final_response, exc_caught)

        return self._read_result()

    # ------------------------------------------------------------------
    # PipelineAgent 覆盖 — 使用 profile 的 max_tool_rounds
    # ------------------------------------------------------------------

    # max_tool_rounds 已在 __init__ 中覆盖，_build_pipeline 自动使用

    # ------------------------------------------------------------------
    # 遗言机制（从 BaseSubAgent 移植，避免多继承）
    # ------------------------------------------------------------------

    def _write_result(
        self,
        final_response: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """写遗言到 workspace_path/RESULT.md。"""
        result_path = self.workspace_path / _RESULT_FILE
        lines: list[str] = []

        if final_response:
            lines.append(final_response)
            lines.append("")

        if exc is not None:
            lines.append("## 执行异常")
            lines.append("")
            lines.append(f"- 异常类型：`{type(exc).__name__}`")
            lines.append(f"- 异常信息：{exc}")
            lines.append(f"- 工作目录：`{self.workspace_path}`")
            lines.append("")
            lines.append("```")
            lines.append(traceback.format_exc().strip())
            lines.append("```")
            lines.append("")

        lines.append("## 工作目录文件清单")
        lines.append("")
        file_count = 0
        try:
            for root, _dirs, files in os.walk(self.workspace_path):
                for fname in sorted(files):
                    abs_path = Path(root) / fname
                    try:
                        rel = abs_path.relative_to(self.workspace_path)
                    except ValueError:
                        rel = abs_path
                    size = abs_path.stat().st_size
                    lines.append(f"- `{rel}` ({size} bytes)")
                    file_count += 1
        except Exception as walk_exc:
            lines.append(f"- （文件清单遍历失败：{walk_exc}）")

        if file_count == 0:
            lines.append("- （工作目录为空）")
        lines.append("")

        try:
            self.workspace_path.mkdir(parents=True, exist_ok=True)
            result_path.write_text("\n".join(lines), encoding="utf-8")
            _rlog.info(
                f"session_{self.session_id}",
                f"[UniversalSubAgent] 遗言已写入: {result_path} "
                f"(文件数={file_count}, 有异常={exc is not None})",
            )
        except Exception as write_exc:
            _rlog.error(
                f"session_{self.session_id}",
                f"[UniversalSubAgent] 遗言写入失败: {write_exc}",
            )

    def _read_result(self) -> str:
        """读取 RESULT.md 内容。"""
        result_path = self.workspace_path / _RESULT_FILE
        try:
            return result_path.read_text(encoding="utf-8")
        except Exception as e:
            fallback = (
                f"[子 Agent {self.subagent_id}] RESULT.md 读取失败: {e}\n"
                f"工作目录: {self.workspace_path}"
            )
            _rlog.error(f"session_{self.session_id}", f"[UniversalSubAgent] {fallback}")
            return fallback

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def profile(self) -> ToolAgentConfig:
        return self._profile
