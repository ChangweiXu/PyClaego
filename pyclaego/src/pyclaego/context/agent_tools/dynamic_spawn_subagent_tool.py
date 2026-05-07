"""DynamicSpawnSubagentTool — 配置驱动的子代理派遣工具

与 SpawnSubagentTool 的区别：
  - get_description() 动态遍历 SUBAGENT_PROFILES 生成 LLM 工具描述
    （新增子代理类型无需修改此文件）
  - _build_context() 使用 ConfigurableSubAgentContext（单一通用类）
    替代 if/elif 分支创建不同类型 ContextHandler
  - execute() 通过 AgentFactory 创建 UniversalSubAgent
  - 所有差异化行为通过 ToolAgentConfig 驱动
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...llm import UnifiedMessage
from ...logging import get_running_log
from ...task_manager import SessionTaskHandlerV2, TaskType
from ...tool.base_tool import ToolResult
from ...tool_agent import SUBAGENT_PROFILES, resolve_profile
from ..subagent.configurable_subagent_context import ConfigurableSubAgentContext
from .spawn_subagent_tool import SpawnSubagentTool

_rlog = get_running_log()


class DynamicSpawnSubagentTool(SpawnSubagentTool):
    """配置驱动的子代理派遣工具。

    继承 SpawnSubagentTool 以复用工作目录创建、TaskHandler 管理等基础设施，
    但 get_description() 和 _build_context() 完全由 ToolAgentConfig 驱动。
    """

    # ------------------------------------------------------------------
    # LLM 工具描述 — 动态生成
    # ------------------------------------------------------------------

    def get_description(self) -> dict[str, Any]:
        """动态遍历 SUBAGENT_PROFILES 生成 subagent_type 选项描述。

        每次调用都实时遍历注册表，因此运行时注册的新类型会自动生效。
        """
        type_options: list[str] = []
        for name, profile in SUBAGENT_PROFILES.items():
            if profile.uses_all_tools:
                tools_desc = "全部工具"
            elif profile.allowed_tools:
                tools_desc = ", ".join(sorted(profile.allowed_tools))
            else:
                tools_desc = "无工具（单次 LLM 调用）"
            type_options.append(
                f'  "{name}"：{profile.description}\n'
                f'    可用工具：{tools_desc}。'
                f'最大轮次：{profile.max_tool_rounds}。'
            )

        return {
            "name": "spawn_subagent",
            "description": (
                "创建并执行一个子 Agent 任务。"
                "子 Agent 拥有独立的工作目录，可以自由读写其中的文件。"
                "任务完成后，子 Agent 的最终输出和工作目录文件清单将作为结果返回。"
                "子 Agent 的唯一标识由系统自动生成，无需指定。"
            ),
            "parameters": {
                "task_prompt": {
                    "type": "string",
                    "description": "传递给子 Agent 的完整任务描述，尽量详细。",
                    "required": True,
                },
                "subagent_type": {
                    "type": "string",
                    "description": (
                        "子 Agent 类型，决定其工具集和执行策略。可选值：\n"
                        + "\n".join(type_options)
                    ),
                    "required": True,
                },
                "memory_mode": {
                    "type": "string",
                    "description": (
                        "上下文记忆模式：\n"
                        "  \"empty\"（默认）：子 Agent 从零开始，仅有子 Agent 专属系统提示词。\n"
                        "  \"inherit\"：子 Agent 继承当前对话历史（剔除最后一轮工具调用，"
                        "并将任务描述追加到更早的用户消息末尾）。"
                    ),
                    "required": False,
                },
            },
        }

    # ------------------------------------------------------------------
    # 上下文构建 — 使用 ConfigurableSubAgentContext
    # ------------------------------------------------------------------

    def _build_context(
        self,
        workspace_path: Path,
        memory_mode: str,
        task_prompt: str,
        parent_context_snapshot: dict[str, Any] | None,
        subagent_task_handler: SessionTaskHandlerV2,
        subagent_type: str = "echo",
    ):
        """使用 ConfigurableSubAgentContext 构建子代理上下文。

        替代父类的 if/elif 分支，通过 ToolAgentConfig 驱动。
        """
        # 查找并解析 profile（合并 widget 级 YAML 覆盖 + 父 Agent LLM 继承）
        try:
            profile = resolve_profile(subagent_type, self.base_agent_config)
        except KeyError:
            available = list(SUBAGENT_PROFILES.keys())
            raise ValueError(
                f"未知的子代理类型: '{subagent_type}'。"
                f"可用类型: {available}"
            )

        # ── "fork" 上下文策略：继承父 Agent 全部消息 ─────────────────
        # fork 与 inherit 的区别：
        #   - inherit: LLM 主动选择是否继承（通过 memory_mode 参数）
        #   - fork:    由 profile 决定，总是继承父消息，子代理从父上下文分叉
        if profile.context_strategy == "fork" and memory_mode != "inherit":
            memory_mode = "inherit"
            _rlog.info(
                f"session_{self.session_id}",
                f"[DynamicSpawnSubagentTool] profile '{subagent_type}' "
                f"context_strategy='fork' → 自动设置 memory_mode='inherit'",
            )

        # 包装 widget_config
        sub_context_cfg: dict[str, Any] = dict(
            self.widget_config.get("context_subagents") or {}
        )
        sub_context_cfg["type"] = subagent_type

        sub_widget_cfg: dict[str, Any] = {
            "context": sub_context_cfg,
            "ps_metadata": self.widget_config.get("ps_metadata", {}),
        }

        # inherit 模式：处理父上下文消息
        initial_messages: list[UnifiedMessage] = []
        initial_system: str | None = None

        if memory_mode == "inherit" and parent_context_snapshot:
            raw_messages: list[UnifiedMessage] = list(
                parent_context_snapshot.get("messages", [])
            )

            if raw_messages and raw_messages[-1].role == "assistant":
                raw_messages = raw_messages[:-1]

            for i in range(len(raw_messages) - 1, -1, -1):
                if raw_messages[i].role == "user":
                    original_text = raw_messages[i].text or ""
                    appended_text = (
                        original_text
                        + f"\n\n---\n**子任务补充说明**\n{task_prompt}"
                    )
                    raw_messages[i] = UnifiedMessage(
                        role="user",
                        text=appended_text,
                        tool_results=raw_messages[i].tool_results,
                    )
                    break

            initial_messages = raw_messages

        # 提取 project_root（供 code_explorer 等使用）
        project_root = self.widget_config.get("ps_metadata", {}).get(
            "project_root", "."
        )

        return ConfigurableSubAgentContext(
            session_id=self.session_id,
            workspace_path=workspace_path,
            config=sub_widget_cfg,
            profile=profile,
            memory_mode=memory_mode,
            initial_messages=initial_messages if memory_mode == "inherit" else [],
            initial_system=initial_system,
            project_root=project_root,
            session_task_handler=subagent_task_handler,
        )

    # ------------------------------------------------------------------
    # 执行入口 — 复用父类 execute()，但通过 AgentFactory 创建 UniversalSubAgent
    # ------------------------------------------------------------------

    async def execute(
        self,
        task_prompt: str,
        subagent_type: str,
        memory_mode: str = "empty",
        parent_context_snapshot: dict[str, Any] | None = None,
        session_task_handler: SessionTaskHandlerV2 | None = None,
        **kwargs,
    ) -> ToolResult:
        """创建并执行子 Agent，返回其 RESULT.md 内容。

        与父类的主要区别：使用 ConfigurableSubAgentContext + UniversalSubAgent。
        """
        if not session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        # ── 1. 自动生成 subagent_id ────────────────────────────────────
        subagent_id = self.generate_subagent_id()
        await session_task_handler.log_info(
            f"[DynamicSpawnSubagentTool] 自动生成 subagent_id: {subagent_id}",
        )

        # ── 2. 推断工作目录 ────────────────────────────────────────────
        try:
            workspace_path = self._resolve_workspace(subagent_id)
        except Exception as e:
            await session_task_handler.log_error(
                f"[DynamicSpawnSubagentTool] 工作目录推断失败: {e}\n{traceback.format_exc()}",
            )
            return self._fail(
                f"工作目录推断失败: {e}",
                log_tag=f"session_{session_task_handler.get_session_id()}",
            )

        os.makedirs(workspace_path, exist_ok=True)
        await session_task_handler.log_info(
            f"[DynamicSpawnSubagentTool] 子 Agent 工作目录: {workspace_path}",
        )

        # ── 3. 创建 SubAgent 子任务 ────────────────────────────────────
        subagent_task_handler = await session_task_handler.create_subtask(
            task_type=TaskType.SUBAGENT_SPAWN,
            name=f"SubAgent: {subagent_id} ({subagent_type})",
            metadata={
                "subagent_id": subagent_id,
                "subagent_type": subagent_type,
                "memory_mode": memory_mode,
                "llm_id": self.base_agent_config.get("llm", ""),
                "context_strategy": memory_mode,
                "initial_message": task_prompt[:300],
            },
        )
        if not subagent_task_handler:
            await session_task_handler.log_error(
                "[DynamicSpawnSubagentTool] 创建 SubAgent 任务失败",
            )
            return self._fail(
                "创建 SubAgent 任务失败",
                log_tag=f"session_{session_task_handler.get_session_id()}",
            )

        # ── 4. 构建 ConfigurableSubAgentContext ────────────────────────
        sub_context = self._build_context(
            workspace_path=workspace_path,
            memory_mode=memory_mode,
            task_prompt=task_prompt,
            parent_context_snapshot=parent_context_snapshot,
            subagent_task_handler=subagent_task_handler,
            subagent_type=subagent_type,
        )

        # ── 5. 构建任务消息 ────────────────────────────────────────────
        user_message = {
            "role": "user",
            "content": task_prompt,
            "user_id": "spawn_system",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "user",
        }

        # ── 6. 通过 SecurityHandler 安全通道执行 ──────────────────────
        from ...security_executor import SecurityHandler
        security_handler = SecurityHandler.get_instance()

        await subagent_task_handler.log_info(
            f"[DynamicSpawnSubagentTool] 开始执行子 Agent "
            f"(subagent_id={subagent_id}, type={subagent_type}, mode={memory_mode})",
        )

        # ── 6b. 构造子 Agent 流式回调（若有 factory） ───────────────
        stream_callback = None
        _stream_factory = kwargs.get("_stream_factory")
        if callable(_stream_factory):
            stream_callback = _stream_factory(subagent_id)

        result_dict = await security_handler.request_subagent_call(
            session_id=self.session_id,
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            subagent_handler=self.subagent_handler,
            workspace_path=workspace_path,
            base_config=self.base_agent_config,
            context_handler=sub_context,
            user_message=user_message,
            subagent_task_handler=subagent_task_handler,
            stream_callback=stream_callback,
        )

        if result_dict.get("success"):
            output: str = result_dict.get("output", "")
            await subagent_task_handler.log_info(
                f"[DynamicSpawnSubagentTool] 子 Agent 完成 "
                f"(subagent_id={subagent_id}, output_len={len(output)})",
            )
            await subagent_task_handler.complete(result={
                "output_len": len(output),
                "workspace_path": str(workspace_path),
            })

            return self._success(
                output=output,
                metadata={
                    "subagent_id": subagent_id,
                    "subagent_type": subagent_type,
                    "memory_mode": memory_mode,
                    "workspace_path": str(workspace_path),
                },
            )
        else:
            error_msg = result_dict.get("error", "子 Agent 执行失败")
            await subagent_task_handler.fail(error=error_msg)
            return self._fail(
                f"子 Agent '{subagent_id}' 执行失败: {error_msg}",
                log_tag=f"session_{session_task_handler.get_session_id()}",
            )
