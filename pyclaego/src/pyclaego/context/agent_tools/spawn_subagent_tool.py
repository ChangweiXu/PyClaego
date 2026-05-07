"""SpawnSubagentTool — 创建并执行子 Agent 的工具

主 Agent（SpawnAgent）通过此工具请求 LLM 动态创建子任务执行者。
工具内部完成子 Agent 的工作目录创建、上下文初始化和执行驱动。
子 Agent 的唯一标识（subagent_id）由系统自动生成，格式为 YYYYMMDD_HHMMSS_xxxxxx。

LLM 工具定义（供主 Agent 的 LLM 使用）：
  name: spawn_subagent
  parameters:
    task_prompt   (str, required): 传递给子 Agent 的完整任务描述
    subagent_type (str, required): 子 Agent 类型（须在 SUBAGENT_REGISTRY 中注册）
    memory_mode   (str, optional): 上下文模式，"empty"（默认）| "inherit"

execute() 的非 LLM 参数（由 SpawnAgent 在调用时注入，不暴露给 LLM）：
    parent_context_snapshot: inherit 模式下父 Agent 上下文快照
    subagent_handler:        进度通知回调（会附加 subagent_id 标记后转发）
"""

import os
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...llm import ToolDefinition, UnifiedMessage
from ...task_manager import SessionTaskHandlerV2, TaskType
from ...tool.base_tool import ToolResult
from .base_agent_tool import AgentBaseTool


class SpawnSubagentTool(AgentBaseTool):
    """创建并执行子 Agent 的工具（继承 AgentBaseTool）

    IS_READONLY = False（创建工作目录，驱动子 Agent 执行）
    IS_PARALLELIZABLE = True（多个子 Agent 可并发执行，各自工作目录独立）
    """

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = True

    @staticmethod
    def generate_subagent_id() -> str:
        """生成唯一的子Agent标识
        
        格式: YYYYMMDD_HHMMSS_{6位UUID}
        示例: 20260409_212736_a3f8c1
        
        Returns:
            str: 唯一的subagent_id字符串
        """
        import uuid
        from datetime import datetime
        
        # 生成时间戳部分
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 生成6位UUID后缀
        uuid_suffix = uuid.uuid4().hex[:6]
        
        return f"{timestamp}_{uuid_suffix}"

    def __init__(
        self,
        tool_config: dict[str, Any],
        subagent_handler: Callable,
        session_id: str,
        base_agent_config: dict[str, Any],
        widget_config: dict[str, Any],
        widget_workspace: Path,
    ) -> None:
        # 若调用方未在 tool_config 里填写 tool_type / tool_name，补充默认值
        merged_config = {
            "tool_type": "spawn_subagent",
            "tool_name": "spawn_subagent",
            "enabled": True,
            **tool_config,
        }
        super().__init__(merged_config, subagent_handler, session_id, base_agent_config)
        # widget_config 是完整 widget 配置（含 context_subagents / ps_metadata 等）
        # widget_workspace 是当前 Widget 的 workspace 目录，子 Agent 在其下建立 subagents/<sid>/
        self.widget_config: dict[str, Any] = widget_config or {}
        self.widget_workspace: Path = widget_workspace

    # ------------------------------------------------------------------
    # LLM 工具协议：get_description
    # ------------------------------------------------------------------

    def get_description(self) -> dict[str, Any]:
        """返回供 tool_description_to_definition 转换的工具描述字典"""
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
                        "  \"echo\"：最简单的子 Agent，单次 LLM 调用，无工具，无循环。"
                        "适合纯文本生成、改写、摘要等不需要外部资源的任务。\n"
                        "  \"info_gatherer\"：信息收集子 Agent，支持完整的 Agent-Tool-Loop。"
                        "可用工具：glob（文件匹配）、search_text（文本搜索）、"
                        "list_directory（目录列举）、read_file（读文件）、"
                        "write_file（写文件，仅限工作目录）、mkdir（创建目录，仅限工作目录）、"
                        "download_file（下载文件，仅限工作目录）、"
                        "web_search（网络搜索）、web_fetch（网页抓取）。"
                        "适合需要读取文件、搜索信息、抓取网页、整理并保存结果的任务。"
                        "不支持执行代码（无 bash 工具）。\n"
                        "  \"code_explorer\"：代码探索子 Agent，专门用于阅读和理解代码库。"
                        "支持完整的 Agent-Tool-Loop，工具集限制为只读文件浏览工具："
                        "glob、list_directory、read_file、search_text、find_line、file_info。"
                        "可在工作目录内写入 RESULT.md 和笔记（write_file、mkdir）。"
                        "探索目标目录由 session_metadata.project_root 配置决定。"
                        "适合 explore the repo / summarize the module / explain the workflow 等任务。"
                        "不支持网络工具和代码执行。"
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
            "examples": [
                {
                    "task_prompt": "搜索近期关于量子计算的最新进展，整理成摘要，保存到 RESULT.md",
                    "subagent_type": "info_gatherer",
                    "memory_mode": "empty",
                },
                {
                    "task_prompt": "将以下内容改写为正式书面风格：...",
                    "subagent_type": "echo",
                    "memory_mode": "empty",
                },
                {
                    "task_prompt": "探索 src/agent/ 模块，总结其类层次结构、核心工作流和设计模式",
                    "subagent_type": "code_explorer",
                    "memory_mode": "empty",
                },
            ],
        }

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------

    async def execute(
        self,
        task_prompt: str,
        subagent_type: str,
        memory_mode: str = "empty",
        # 以下不暴露给 LLM，由 SpawnAgent 在调用时注入
        parent_context_snapshot: dict[str, Any] | None = None,
        session_task_handler: SessionTaskHandlerV2 | None = None,
        **kwargs,
    ) -> ToolResult:
        """创建并执行子 Agent，返回其 RESULT.md 内容

        Args:
            task_prompt:             传递给子 Agent 的任务描述
            subagent_type:           子 Agent 类型（在 SUBAGENT_REGISTRY 中注册）
            memory_mode:             "empty" | "inherit"
            parent_context_snapshot: inherit 模式下父 Context 的快照 dict
                                     格式: {"system": str|None, "messages": List[UnifiedMessage], ...}
            session_task_handler:    进度通知回调（附加 subagent_id 后转发）
            **kwargs:                额外透传参数（忽略）

        Returns:
            ToolResult（output = RESULT.md 全文字符串）
        """
        if not session_task_handler:
            raise ValueError("SessionTaskHandlerV2 实例未传入")

        # ── 1. 自动生成 subagent_id ────────────────────────────────────
        subagent_id = self.generate_subagent_id()
        await session_task_handler.log_info(
            f"[SpawnSubagentTool] 自动生成 subagent_id: {subagent_id}",
        )

        # ── 2. 推断工作目录 ────────────────────────────────────────────
        try:
            workspace_path = self._resolve_workspace(subagent_id)
        except Exception as e:
            await session_task_handler.log_error(
                f"[SpawnSubagentTool] 工作目录推断失败: {e}\n{traceback.format_exc()}",
            )
            return self._fail(
                f"工作目录推断失败: {e}",
                log_tag=f"session_{session_task_handler.get_session_id()}",
            )

        os.makedirs(workspace_path, exist_ok=True)
        await session_task_handler.log_info(
            f"[SpawnSubagentTool] 子 Agent 工作目录: {workspace_path}",
        )

        # ── 3. 构建带子 Agent 标记的进度回调 ──────────────────────────
        # 【2026年04月10日新增】通知 TaskManager: SubAgent Spawn 开始
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
                "[SpawnSubagentTool] 创建 SubAgent 任务失败",
            )
            return self._fail(
                "创建 SubAgent 任务失败",
                log_tag=f"session_{session_task_handler.get_session_id()}",
            )

        # ── 4. 构建子 Agent 上下文处理器 ──────────────────────────────
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

        # ── 6. 调用 SecurityHandler.request_subagent_call() 走安全通道 ─
        from ...security_executor import SecurityHandler
        security_handler = SecurityHandler.get_instance()

        await subagent_task_handler.log_info(
            f"[SpawnSubagentTool] 开始执行子 Agent "
            f"(subagent_id={subagent_id}, type={subagent_type}, mode={memory_mode})",
        )

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
        )

        if result_dict.get("success"):
            output: str = result_dict.get("output", "")
            await subagent_task_handler.log_info(
                f"[SpawnSubagentTool] 子 Agent 完成 "
                f"(subagent_id={subagent_id}, output_len={len(output)})",
            )
            # 【2026年04月10日新增】通知 TaskManager: SubAgent 任务完成
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
            # 在 SecurityHandler.request_subagent_call() 已经记录 error 信息
            # 此处无需重复记录
            error_msg = result_dict.get("error", "子 Agent 执行失败")
            await subagent_task_handler.fail(error=error_msg)
            return self._fail(
                f"子 Agent '{subagent_id}' 执行失败: {error_msg}",
                log_tag=f"session_{session_task_handler.get_session_id()}",
            )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _resolve_workspace(self, subagent_id: str) -> Path:
        """在当前 Widget 的 workspace 下建立子 Agent 子目录"""
        return self.widget_workspace / "subagents" / subagent_id

    def _build_context(
        self,
        workspace_path: Path,
        memory_mode: str,
        task_prompt: str,
        parent_context_snapshot: dict[str, Any] | None,
        subagent_task_handler: SessionTaskHandlerV2,
        subagent_type: str = "echo",
    ):
        """构建适合 subagent_type 的 ContextHandler 实例"""
        from ..subagent import BaseSubAgentContextHandler

        # 从 widget_config 提取 context_subagents 配置切片，作为子 Agent 的 context
        sub_context_cfg: dict[str, Any] = dict(
            self.widget_config.get("context_subagents") or {}
        )
        sub_context_cfg["type"] = subagent_type  # 选体

        # 包装为 widget_config 形状，以供 BaseContextHandler 中心重绑奇市 self.config
        sub_widget_cfg: dict[str, Any] = {
            "context": sub_context_cfg,
            # 子 Agent 继承父 widget 的 ps_metadata（供 project_root 等使用）
            "ps_metadata": self.widget_config.get("ps_metadata", {}),
        }

        initial_messages: list[UnifiedMessage] = []
        initial_system: str | None = None

        if memory_mode == "inherit" and parent_context_snapshot:
            # 父 Context 快照处理：
            # 1. 丢弃最后一条 assistant 消息（即发起 spawn 的那轮工具调用消息）
            # 2. 将 task_prompt 追加到更早一条 user 消息内容末尾
            # 3. 替换系统提示词（不继承父 Agent 的系统提示词）
            raw_messages: list[UnifiedMessage] = list(
                parent_context_snapshot.get("messages", [])
            )

            # 丢弃最后一条 assistant 消息
            if raw_messages and raw_messages[-1].role == "assistant":
                raw_messages = raw_messages[:-1]

            # 将 task_prompt 追加到最后一条 user 消息末尾
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
            # 系统提示词不继承，使用子 Agent 默认模板（initial_system=None 时由 Context 用默认值）

        if subagent_type == "code_explorer":
            project_root = self.widget_config.get("ps_metadata", {}).get(
                "project_root", "."
            )
            from ..subagent import CodeExplorerContextHandler
            return CodeExplorerContextHandler(
                session_id=self.session_id,
                workspace_path=workspace_path,
                config=sub_widget_cfg,
                project_root=project_root,
                memory_mode=memory_mode,
                initial_messages=initial_messages if memory_mode == "inherit" else [],
                initial_system=initial_system,
                session_task_handler=subagent_task_handler,
            )

        if subagent_type == "info_gatherer":
            from ..subagent import InfoGathererContextHandler
            return InfoGathererContextHandler(
                session_id=self.session_id,
                workspace_path=workspace_path,
                config=sub_widget_cfg,
                memory_mode=memory_mode,
                initial_messages=initial_messages if memory_mode == "inherit" else [],
                initial_system=initial_system,
                session_task_handler=subagent_task_handler,
            )

        return BaseSubAgentContextHandler(
            session_id=self.session_id,
            workspace_path=workspace_path,
            config=sub_widget_cfg,
            memory_mode=memory_mode,
            initial_messages=initial_messages if memory_mode == "inherit" else [],
            initial_system=initial_system,
            session_task_handler=subagent_task_handler,
        )

    # ------------------------------------------------------------------
    # ToolDefinition 便捷方法（供 SpawnAgent 调用）
    # ------------------------------------------------------------------

    def to_tool_definition(self) -> ToolDefinition:
        """将工具描述转换为 ToolDefinition（供 LLM 工具列表使用）"""
        from ...llm import tool_description_to_definition
        return tool_description_to_definition(self.get_description())
