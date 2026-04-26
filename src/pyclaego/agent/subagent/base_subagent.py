"""BaseSubAgent — 短生命周期子 Agent 基类

子 Agent 由主 Agent（SpawnAgent）通过 spawn_subagent 工具动态创建，
拥有独立的工作目录（session_workspace/subagents/<subagent_id>/）。

关键约定：
- 必须通过 AgentFactory.create_subagent() 创建，不可直接注册为主 Agent 类型。
- subagent_id 用于 SecurityHandler 日志标识，同时决定工作目录路径。
- process() 的 finally 块必须负责写"遗言"（RESULT.md），
  确保即使执行异常也能留下状态报告，供主 Agent 判断后续处理方式。

遗言文件（RESULT.md）内容构成：
  1. LLM 最终返回的文本（final_response），无格式强制
  2. 追加自动生成的工作目录文件清单（## 工作目录文件清单）
  3. 若过程以异常结束，追加 ## 执行异常 段，说明异常类型和工作路径

process() 必须返回 RESULT.md 的完整内容字符串，由 SpawnSubagentTool 作为
ToolResult.output 回传给主 Agent 的 LLM。
"""

import json
import os
import traceback
from abc import abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from ..base_agent import BaseAgent
from ...context.subagent import BaseSubAgentContextHandler
from ...task_manager import SessionTaskHandlerV2, TaskType
from ...llm import ToolCall, ToolCallResult
from ...logging import get_running_log

_rlog = get_running_log()

_RESULT_FILE = "RESULT.md"


class BaseSubAgent(BaseAgent):
    """短生命周期子 Agent 基类

    Args:
        config:         Agent 配置字典（继承自主 Agent，含 llm 等字段）
        session_id:     父会话 ID（用于 SecurityHandler 日志和 LLM 调用记录）
        subagent_id:    子 Agent 唯一标识（由 SpawnSubagentTool 自动生成，格式: YYYYMMDD_HHMMSS_xxxxxx）
        workspace_path: 子 Agent 独立工作目录（已由 SpawnSubagentTool 创建）
    """

    def __init__(
        self,
        config: Dict[str, Any],
        session_id: str,
        subagent_id: str,
        workspace_path: Path,
    ) -> None:
        super().__init__(config, session_id)
        self.subagent_id: str = subagent_id
        self.workspace_path: Path = Path(workspace_path)

        _rlog.info(
            f"session_{session_id}",
            f"[BaseSubAgent] 子 Agent 初始化完成 "
            f"(subagent_id={subagent_id}, workspace={self.workspace_path})",
        )

    # ------------------------------------------------------------------
    # 抽象入口 — 子类实现具体 LLM 循环逻辑
    # ------------------------------------------------------------------

    @abstractmethod
    async def process(
        self,
        user_message: Dict[str, Any],
        context_handler: BaseSubAgentContextHandler,
        subagent_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        """执行子 Agent 主逻辑并返回 RESULT.md 全文。

        子类必须：
        1. 在 try 块中完成 LLM 调用（和工具调用）循环，将最终回复赋值给 final_response。
        2. 在 finally 块中调用 self._write_result(final_response, exc) 写遗言。
        3. 返回 self._read_result()（RESULT.md 全文）。

        Args:
            user_message:    用户（或主 Agent）传入的任务消息 dict
            context_handler: BaseSubAgentContextHandler 实例（每个子 Agent 独立）
            user_id:         固定传 "spawn_system"
            **kwargs:        额外透传参数（如 msg_update_handler）

        Returns:
            RESULT.md 全文字符串
        """
        ...

    async def process_v2(
        self,
        user_message: Dict[str, Any],
        context_handler: BaseSubAgentContextHandler,
        subagent_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        return await self.process(user_message, context_handler, subagent_task_handler, **kwargs)

    # ------------------------------------------------------------------
    # 遗言工具方法（供子类 finally 块调用）
    # ------------------------------------------------------------------

    def _write_result(
        self,
        final_response: Optional[str] = None,
        exc: Optional[BaseException] = None,
    ) -> None:
        """写遗言到 workspace_path/RESULT.md。

        Args:
            final_response: LLM 最终回复文本（正常结束时传入）
            exc:            捕获到的异常对象（异常结束时传入）
        """
        result_path = self.workspace_path / _RESULT_FILE
        lines: list[str] = []

        # ── 1. 主体内容 ────────────────────────────────────────────────
        if final_response:
            lines.append(final_response)
            lines.append("")

        # ── 2. 异常信息 ────────────────────────────────────────────────
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

        # ── 3. 工作目录文件清单 ─────────────────────────────────────────
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

        # ── 写盘 ───────────────────────────────────────────────────────
        try:
            self.workspace_path.mkdir(parents=True, exist_ok=True)
            result_path.write_text("\n".join(lines), encoding="utf-8")
            _rlog.info(
                f"session_{self.session_id}",
                f"[BaseSubAgent] 遗言已写入: {result_path} "
                f"(文件数={file_count}, 有异常={exc is not None})",
            )
        except Exception as write_exc:
            _rlog.error(
                f"session_{self.session_id}",
                f"[BaseSubAgent] 遗言写入失败: {write_exc}",
            )

    def _read_result(self) -> str:
        """读取 RESULT.md 内容，写入失败时返回兜底字符串。"""
        result_path = self.workspace_path / _RESULT_FILE
        try:
            return result_path.read_text(encoding="utf-8")
        except Exception as e:
            fallback = (
                f"[子 Agent {self.subagent_id}] RESULT.md 读取失败: {e}\n"
                f"工作目录: {self.workspace_path}"
            )
            _rlog.error(f"session_{self.session_id}", f"[BaseSubAgent] {fallback}")
            return fallback

    # ------------------------------------------------------------------
    # 迭代记录工具方法（供子类每轮循环后调用）
    # ------------------------------------------------------------------

    def _write_loop_json(
        self,
        round_count: int,
        text_reply: Optional[str],
        tool_calls: Optional[List[ToolCall]],
        tool_results: Optional[List[ToolCallResult]],
    ) -> None:
        """将单轮循环数据写入 workspace_path/loop-{round_count}.json。

        每次工具调用轮次结束后调用，无论循环是否最终成功，
        文件均保留在工作目录中供主 Agent 事后回溯。

        Args:
            round_count:  当前轮次编号（从 1 开始）
            text_reply:   本轮 LLM 返回的文本内容
            tool_calls:   本轮发起的工具调用列表（无工具时为 None 或空列表）
            tool_results: 本轮工具执行结果列表（无工具时为 None 或空列表）
        """
        loop_path = self.workspace_path / f"loop-{round_count}.json"
        data: Dict[str, Any] = {
            "round": round_count,
            "timestamp": datetime.now().isoformat(),
            "text_reply": text_reply or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in (tool_calls or [])
            ],
            "tool_results": [
                {
                    "tool_call_id": tr.tool_call_id,
                    "tool_name": tr.tool_name,
                    "content": tr.content,
                }
                for tr in (tool_results or [])
            ],
        }
        try:
            self.workspace_path.mkdir(parents=True, exist_ok=True)
            loop_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _rlog.info(
                f"session_{self.session_id}",
                f"[BaseSubAgent] loop-{round_count}.json 已写入: {loop_path}",
            )
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[BaseSubAgent] loop-{round_count}.json 写入失败: {e}",
            )

    async def _summary_call(
        self,
        system: Optional[str],
        messages: list,
        round_count: int,
        subagent_task_handler: SessionTaskHandlerV2,
    ) -> str:
        """达到最大轮次后发起最终汇总 LLM 调用（无工具）。

        追加一条 user 消息，要求 LLM 总结本次探索过程、得出结论，
        并指出哪些 loop-N.json 文件包含关键细节供主 Agent 参考。

        Args:
            system:                 系统提示词（与主循环相同）
            messages:               当前完整消息列表（含所有工具往返）
            round_count:            已执行的总轮次数
            subagent_task_handler:  用于创建子任务和记录日志

        Returns:
            LLM 汇总文本；失败时返回错误说明字符串
        """
        from ...security_executor import SecurityHandler

        summary_handler = None
        try:
            summary_handler = await subagent_task_handler.create_subtask(
                task_type=TaskType.LLM_CALL,
                name="Summary Call",
                metadata={"round": "summary", "total_rounds": round_count},
            )
            await summary_handler.log_info(
                f"[BaseSubAgent] 发起汇总 LLM 调用（共 {round_count} 轮）"
            )

            # 在现有消息末尾追加汇总指令
            from ...llm import UnifiedMessage

            summary_prompt = (
                f"你已执行了 {round_count} 轮工具调用，现已达到最大轮次限制。\n"
                f"请对本次任务做一份完整的汇总报告，内容包括：\n"
                f"1. 任务目标回顾\n"
                f"2. 主要发现与结论（以结构化方式列出）\n"
                f"3. 指出哪些轮次（loop-N.json，N 从 1 开始）包含关键细节，"
                f"   供主 Agent 或人工在工作目录中查阅\n"
                f"4. 若任务未完成，说明原因及建议的后续步骤\n\n"
                f"工作目录中已按轮次保存了 loop-1.json … loop-{round_count}.json，"
                f"请在报告中引用具体文件名。"
            )
            summary_messages = list(messages) + [
                UnifiedMessage(role="user", text=summary_prompt)
            ]

            security_handler = SecurityHandler.get_instance()
            llm_result = await security_handler.request_subagent_llm_call(
                session_id=self.session_id,
                subagent_id=self.subagent_id,
                llm_id=self.get_llm_id(),
                system=system,
                messages=summary_messages,
                task_handler=summary_handler,
                tool_list=None,
            )

            if not llm_result["success"]:
                error_msg = llm_result.get("error", "Unknown error")
                await summary_handler.log_error(
                    f"[BaseSubAgent] 汇总 LLM 调用失败: {error_msg}"
                )
                return f"[ERROR] 汇总调用失败: {error_msg}"

            summary_text = llm_result["v2_response"].text or "（汇总回复为空）"
            await summary_handler.log_info(
                f"[BaseSubAgent] 汇总完成: `{summary_text[:60]}...`"
            )
            await summary_handler.complete()
            return summary_text

        except Exception as e:
            if summary_handler:
                await summary_handler.log_error(
                    f"[BaseSubAgent] 汇总调用异常: {e}\n{traceback.format_exc()}"
                )
            return f"[ERROR] 汇总调用异常: {str(e)}"

    # ------------------------------------------------------------------
    # get_info 重写（附加子 Agent 信息）
    # ------------------------------------------------------------------

    def get_info(self) -> Dict[str, Any]:
        info = super().get_info()
        info["subagent_id"] = self.subagent_id
        info["workspace_path"] = str(self.workspace_path)
        return info
