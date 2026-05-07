
import asyncio
import traceback
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import get_config
from ..llm import (
    ChatResponseV2,
    StreamChunk,
    ToolDefinition,
    UnifiedMessage,
    serialize_llm_response,
    summarize_content_parts,
)
from ..logging import get_running_log
from ..skill import get_skill_manager
from ..task_manager import ArtifactReporter, SessionTaskHandlerV2, TaskType
from .monitor import SecurityMonitor
from .query_service import CANCEL_SENTINEL, Choice, PendingQuery, get_query_service
from .record_store import RecordStore

_rlog = get_running_log()


# 文件副作用工具白名单：tool_name -> 参数名（参数值即被改动的路径）
# 命中后在 request_tool_call_v2 / request_subagent_tool_call 成功时附加 KIND_FILE_EDIT 工件
_FILE_EDIT_TOOLS: dict[str, str] = {
    "write_file":     "path",
    "file_edit":      "path",
    "file_delete":    "path",
    "mkdir":          "path",
    "copy_move":      "dst_path",
    "download_file":  "save_path",
}


def _safe_attach(reporter: ArtifactReporter | None, fn_name: str, *args, **kwargs) -> None:
    """安全调用 reporter 的某个方法，捕获并降级日志，绝不打断主流程。"""
    if reporter is None:
        return
    try:
        getattr(reporter, fn_name)(*args, **kwargs)
    except Exception as e:
        _rlog.debug("core_service", f"[SecurityHandler] artifact attach ({fn_name}) skipped: {e}")


class SecurityHandler:
    """安全处理器 - 提供给 Agent 使用的接口（单例模式）
    
    功能：
    - 封装 LLM 调用申请
    - 封装工具调用申请
    - 与 SecurityMonitor 通信
    """
    
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化安全处理器（仅在第一次创建时执行）"""
        if self._initialized:
            return
        
        # 创建 SecurityMonitor 实例
        self.security_monitor = SecurityMonitor()
        
        # LLM 客户端字典（按 llm_id 存储）
        self._llm_clients = {}
        
        # 记录存储（含 TTL 自动清理）
        self.record_store = RecordStore()
        self.record_store.start()

        # 【2026年04月03日21:18:00新增】初始化 SkillManager（单例）
        self.skill_manager = get_skill_manager()
        # 自动加载技能
        try:
            success, failed = self.skill_manager.load_skills()
            _rlog.info("core_service", f"[SecurityHandler] 技能加载完成: 成功 {success}, 失败 {failed}")
        except Exception as e:
            _rlog.error("core_service", f"[SecurityHandler] 技能加载失败: {e}\n{traceback.format_exc()}")
        
        # 【PathResolver 已移除】widget 化后路径占位符 ({{WORKSPACE}} / {{PROJECT}} /
        # {{SKILL:name}}) 不再在此处解析，需要者请在上层传入实际路径。

        self._initialized = True
        _rlog.info("core_service", "[SecurityHandler] 单例实例已初始化")
    
    @classmethod
    def get_instance(cls) -> "SecurityHandler":
        """获取 SecurityHandler 单例实例
        
        Returns:
            SecurityHandler 实例
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _get_or_create_llm_client(self, llm_id: str):
        """获取或创建 LLM 客户端
        
        Args:
            llm_id: LLM 配置 ID
            
        Returns:
            LLM 客户端实例
        """
        # 如果已经创建过，直接返回
        if llm_id in self._llm_clients:
            return self._llm_clients[llm_id]
        
        # 从配置中获取 LLM 配置
        config = get_config()
        llm_config_dict = config.get("llm", {})
        providers = llm_config_dict.get("providers", {})
        
        if llm_id not in providers:
            raise ValueError(f"LLM 配置 '{llm_id}' 不存在于配置文件中")
        
        llm_config = providers[llm_id]
        
        # 创建 LLM 客户端（使用工厂类）
        from ..llm import LLMClientFactory
        llm_client = LLMClientFactory.create_from_config(llm_config)
        
        # 缓存客户端
        self._llm_clients[llm_id] = llm_client
        _rlog.info("core_service", f"[SecurityHandler] 创建 LLM 客户端: {llm_id}")
        
        return llm_client

    @staticmethod
    def _summarize_content_parts(content_parts: list | None) -> list | None:
        """Delegate to record_store.summarize_content_parts."""
        return summarize_content_parts(content_parts)

    @staticmethod
    def _build_messages_summary(
        system: str | None,
        messages: list[UnifiedMessage],
    ) -> list[dict[str, Any]]:
        """将 system + UnifiedMessage 列表序列化为可记录的摘要格式。

        - text 消息：保留为 content 字段
        - content_parts 消息：ImagePart 的 base64 data 截断为前 32 字符，避免日志膨胀
        - tool_calls / tool_results：完整保留
        """
        result: list[dict[str, Any]] = []
        if system:
            result.append({"role": "system", "content": system})
        for m in messages:
            entry: dict[str, Any] = {"role": m.role}
            if m.content_parts:
                serialized_parts = []
                for p in m.content_parts:
                    if p.type == "image":
                        serialized_parts.append({
                            "type": "image",
                            "source_type": p.source_type,
                            "media_type": p.media_type,
                            "data": p.data[:32] + "...[truncated]" if len(p.data) > 32 else p.data,
                        })
                    elif p.type == "document":
                        serialized_parts.append({
                            "type": "document",
                            "source_type": p.source_type,
                            "media_type": p.media_type,
                            "data": p.data[:32] + "...[truncated]" if len(p.data) > 32 else p.data,
                        })
                    else:
                        serialized_parts.append({"type": "text", "text": p.text})
                entry["content_parts"] = serialized_parts
            elif m.text:
                entry["content"] = m.text
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in m.tool_calls
                ]
            if m.tool_results:
                entry["tool_results"] = [
                    {
                        "tool_call_id": tr.tool_call_id,
                        "content": tr.content,
                        "content_parts_summary": SecurityHandler._summarize_content_parts(tr.content_parts),
                    }
                    for tr in m.tool_results
                ]
            result.append(entry)
        return result

    @staticmethod
    def _serialize_llm_response(response: Any) -> Any:
        """Delegate to record_store.serialize_llm_response."""
        return serialize_llm_response(response)

    def _save_llm_call_record(self, **kwargs) -> None:
        raise NotImplementedError("请使用 _save_llm_call_record_v2 方法，支持更多参数和功能")

    def _save_llm_call_record_v2(
        self,
        session_id: str,
        llm_id: str,
        messages: list[dict[str, Any]],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        response: Any = "",
        error: str = "",
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict | None:
        """Delegate to RecordStore.write_llm_call."""
        return self.record_store.write_llm_call(
            session_id=session_id,
            llm_id=llm_id,
            messages=messages,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            security_decision=security_decision,
            success=success,
            response=response,
            error=error,
            tool_list=tool_list,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            kwargs=kwargs,
        )

    def _save_tool_call_record(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        output: str = "",
        error: str = "",
        content_parts: list | None = None,
    ) -> dict | None:
        """Delegate to RecordStore.write_tool_call."""
        return self.record_store.write_tool_call(
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            security_decision=security_decision,
            success=success,
            output=output,
            error=error,
            content_parts=content_parts,
        )
    
    async def request_llm_call(self, **kwargs) -> dict[str, Any]:
        raise NotImplementedError("请使用 request_llm_call_v2 方法，支持更多参数和功能")
    
    async def request_llm_call_v2(
        self,
        session_id: str,
        llm_id: str,
        system: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        **kwargs
    ) -> dict[str, Any]:
        """申请 LLM 调用（V2 版，协议无关统一接口）

        与 request_llm_call 相比，本方法接受语义化入参，由底层 LLM 客户端自行完成
        OpenAI / Anthropic 格式转换。调用方无需感知底层协议差异。

        Args:
            session_id:   会话 ID
            llm_id:       LLM 配置 ID (例如: "kimi_code")
            system:       系统提示词（None 表示不传 system）
            messages:     对话历史，使用统一 UnifiedMessage 格式
            tool_list:    可用工具列表（ToolDefinition 格式），None 表示不使用工具
            temperature:  温度参数（可选，覆盖 LLM 客户端默认值）
            max_tokens:   最大输出 token 数（可选，覆盖 LLM 客户端默认值）
            tool_choice:  工具选择策略：
                          - None / "auto"：LLM 自行决定是否调用工具
                          - "none"：禁用工具调用
                          - "<tool_name>"：强制调用指定工具
            **kwargs:     透传给底层 API 的额外参数

        Returns:
            调用结果字典：
            {
                "success": bool,
                "v2_response": ChatResponseV2 | None,  # 完整响应对象（含 text/tool_calls/stop_reason/usage/raw_response）
                "response": str | None,            # 兼容字段（取 v2_response.text）
                "usage": dict,
                "security_decision": str,
                "error": str | None
            }
        """
        # 安全审查
        review_result = await self.security_monitor.before_llm_call(
            session_id=session_id,
            subagent_id=None,
            llm_id=llm_id,
            system=system,
            messages=messages,
            tool_list=tool_list,
            tool_choices=tool_choice,
            kwargs=kwargs,
        )
        decision = review_result["decision"]

        if decision == "deny":
            _rlog.warning("core_service", f"[SecurityHandler] LLM 调用被拒绝 (session={session_id}): {llm_id} - {review_result['reason']}")
            await self.security_monitor.after_llm_call(
                session_id=session_id,
                subagent_id=None,
                llm_id=llm_id,
                system=system,
                messages=messages,
                tool_list=tool_list,
                tool_choices=tool_choice,
                kwargs=kwargs,
                success=False,
                stop_reason=None,
                error=f"Security check failed: {review_result['reason']}",
            )
            return {
                "success": False,
                "v2_response": None,
                "response": None,
                "usage": {},
                "security_decision": decision,
                "error": f"Security check failed: {review_result['reason']}",
                "log_record": None,
            }

        start_timestamp = datetime.now().isoformat()

        try:
            llm_client = self._get_or_create_llm_client(llm_id)

            v2_resp: ChatResponseV2 = await llm_client.chat_completion_v2(
                system=system,
                messages=messages,
                tool_list=tool_list,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                **kwargs
            )

            end_timestamp = datetime.now().isoformat()

            # 保存调用记录（使用原始响应对象）
            _log_record = self._save_llm_call_record_v2(
                session_id=session_id,
                llm_id=llm_id,
                messages=self._build_messages_summary(system, messages),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                security_decision=decision,
                success=True,
                response=v2_resp.raw_response,
                error="",
                tool_list=tool_list,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                kwargs=kwargs,
            )

            await self.security_monitor.after_llm_call(
                session_id=session_id,
                subagent_id=None,
                llm_id=llm_id,
                system=system,
                messages=messages,
                tool_list=tool_list,
                tool_choices=tool_choice,
                kwargs=kwargs,
                success=True,
                stop_reason=v2_resp.stop_reason,
                error=None,
            )
            return {
                "success": True,
                "v2_response": v2_resp,
                "response": v2_resp.text,   # 兼容旧字段
                "usage": v2_resp.usage,
                "security_decision": decision,
                "error": None,
                "log_record": _log_record,
            }

        except Exception as e:
            end_timestamp = datetime.now().isoformat()

            _log_record = self._save_llm_call_record_v2(
                session_id=session_id,
                llm_id=llm_id,
                messages=self._build_messages_summary(system, messages),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                security_decision=decision,
                success=False,
                response="",
                error=str(e),
                tool_list=tool_list,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                kwargs=kwargs,
            )

            await self.security_monitor.after_llm_call(
                session_id=session_id,
                subagent_id=None,
                llm_id=llm_id,
                system=system,
                messages=messages,
                tool_list=tool_list,
                tool_choices=tool_choice,
                kwargs=kwargs,
                success=False,
                stop_reason=None,
                error=str(e),
            )
            return {
                "success": False,
                "v2_response": None,
                "response": None,
                "usage": {},
                "security_decision": decision,
                "error": str(e),
                "log_record": _log_record,
            }

    async def request_llm_call_v3(
        self,
        session_task_handler: "SessionTaskHandlerV2",
        llm_id: str,
        system: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        stream: bool = False,
        **kwargs
    ) -> dict[str, Any]:
        """申请 LLM 调用（V3 版，支持 SessionTaskHandlerV2 集成）
        
        在此处对多模态消息的内容进行审查，如果非法访问目录或内容不可用，在此处报错拦截
        
        Args:
            session_task_handler: SessionTaskHandlerV2 实例，用于日志和任务管理
            llm_id:       LLM 配置 ID (例如: "kimi_code")
            system:       系统提示词（None 表示不传 system）
            messages:     对话历史，使用统一 UnifiedMessage 格式
            tool_list:    可用工具列表（ToolDefinition 格式），None 表示不使用工具
            temperature:  温度参数（可选，覆盖 LLM 客户端默认值）
            max_tokens:   最大输出 token 数（可选，覆盖 LLM 客户端默认值）
            tool_choice:  工具选择策略
            stream:       是否流式输出（V3 支持）
            **kwargs:     透传给底层 API 的额外参数
            
        Returns:
            调用结果字典（与 request_llm_call_v2 相同格式）：
            {
                "success": bool,
                "response": ChatResponseV2 | None,
                "usage": dict,
                "security_decision": str,
                "error": str | None
            }
        """
        # 从 session_task_handler 提取 session_id
        session_id = session_task_handler.get_session_id()

        llm_task_handler = await session_task_handler.create_subtask(
            task_type=TaskType.LLM_CALL,
            name=(
                f"Session {session_id} LLM Call: "
                f"llm_id={llm_id}, "
                f"num_messages={len(messages)}, "
                f"tools={len(tool_list) if tool_list else 0}"
            ),
            description=f"LLM 调用任务，使用模型 {llm_id}，消息数量 {len(messages)}，工具数量 {len(tool_list) if tool_list else 0}",
            metadata={
                "llm_id": llm_id,
            }
        )
        
        # 记录开始
        await llm_task_handler.log_info(
            f"[SecurityHandler] 开始 LLM 调用 (llm_id={llm_id}, "
            f"msg_count={len(messages)}, tools={len(tool_list) if tool_list else 0})"
        )
        
        # 调用 v2 接口（核心逻辑相同）
        result = await self.request_llm_call_v2(
            session_id=session_id,
            llm_id=llm_id,
            system=system,
            messages=messages,
            tool_list=tool_list,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            **kwargs
        )

        # 工件上报：完整 LLM 响应（懒加载用）。挂到本次 LLM 子任务上。
        try:
            _llm_reporter = ArtifactReporter.for_task(llm_task_handler.get_task_id())
            v2_resp_obj = result.get("v2_response")
            payload: dict[str, Any] = {
                "success": result.get("success"),
                "error": result.get("error"),
                "security_decision": result.get("security_decision"),
                "llm_id": llm_id,
            }
            if v2_resp_obj is not None:
                payload["raw_response"] = self._serialize_llm_response(v2_resp_obj.raw_response)
                payload["text"] = v2_resp_obj.text
                payload["stop_reason"] = v2_resp_obj.stop_reason
                payload["usage"] = v2_resp_obj.usage
                payload["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in (v2_resp_obj.tool_calls or [])
                ]
                payload["produced_by_provider"] = v2_resp_obj.produced_by_provider
                payload["produced_by_model"] = v2_resp_obj.produced_by_model
            log_record = result.get("log_record")
            if log_record is not None:
                payload["record_log"] = log_record
            _safe_attach(_llm_reporter, "llm_response", payload)
            if log_record is not None:
                _safe_attach(_llm_reporter, "meta", "record_log", log_record)
        except Exception as _e:
            _rlog.debug("core_service", f"[SecurityHandler] artifact attach (llm_response v3) skipped: {_e}")

        # 记录结果
        if result["success"] and result.get("v2_response", None):
            v2_resp = result.get("v2_response")
            if v2_resp:
                usage = v2_resp.usage or {}
                tool_names = [tc.name for tc in (v2_resp.tool_calls or [])]
                resp_preview = (v2_resp.text or "")[:50]
                await llm_task_handler.log_info(
                    f"[SecurityHandler] LLM调用成功 | "
                    f"tokens={{prompt:{usage.get('prompt_tokens','?')}, "
                    f"completion:{usage.get('completion_tokens','?')}}} | "
                    f"stop={v2_resp.stop_reason} | "
                    f"response=\"{resp_preview}\" | tools={tool_names}"
                )
                await llm_task_handler.complete({
                    "success": True,
                    "response": v2_resp.text,
                    "usage": v2_resp.usage,
                    "security_decision": result.get("security_decision"),
                    "error": None,
                })
            else:
                raise ValueError("LLM 调用成功但未返回有效响应对象 ChatResponseV2")
        else:
            await llm_task_handler.log_error(
                f"[SecurityHandler] LLM 调用失败: {result.get('error', 'Unknown error')}"
            )
            await llm_task_handler.fail(error=result.get('error', 'Unknown error'))
        
        return result

    async def request_llm_call_stream(
        self,
        session_task_handler: "SessionTaskHandlerV2",
        llm_id: str,
        system: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        **kwargs
    ) -> "AsyncGenerator[StreamChunk, None]":
        """流式 LLM 调用（V3 安全包装版）。

        聚合结果（success/error/usage 等）嵌入在 type="finish" 的最后一个 chunk
        及 generator 关闭后的返回值中。调用方通过 try/finally 收集。

        在流开始前执行安全审查；流结束后记录日志和 artifact。
        安全审查拒绝时 yield 一个 dict（error 占位），然后终止。

        Yields:
            StreamChunk：来自底层 LLM client 的流式响应片段。

        Returns:
            Dict[str, Any]：聚合的调用结果（与 request_llm_call_v3 相同格式）。
        """

        session_id = session_task_handler.get_session_id()

        # ── 安全审查（流开始前）──────────────────────────────────
        review_result = await self.security_monitor.before_llm_call(
            session_id=session_id,
            subagent_id=None,
            llm_id=llm_id,
            system=system,
            messages=messages,
            tool_list=tool_list,
            tool_choices=tool_choice,
            kwargs=kwargs,
        )
        decision = review_result["decision"]

        if decision == "deny":
            _rlog.warning(
                "core_service",
                f"[SecurityHandler] 流式 LLM 调用被拒绝 (session={session_id}): "
                f"{llm_id} - {review_result['reason']}",
            )
            # yield fail chunk（统一流终止协议）
            yield StreamChunk(
                type="fail",
                error_code="security_denied",
                error_message=str(review_result.get("reason", "Security policy denied")),
            )
            return  # async generator 结束，无返回值

        start_timestamp = datetime.now().isoformat()

        # ── 创建 LLM_CALL 子任务 ────────────────────────────────
        llm_task_handler = await session_task_handler.create_subtask(
            task_type=TaskType.LLM_CALL,
            name=(
                f"Session {session_id} LLM Call (stream): "
                f"llm_id={llm_id}, "
                f"num_messages={len(messages)}, "
                f"tools={len(tool_list) if tool_list else 0}"
            ),
            description=f"流式 LLM 调用任务，使用模型 {llm_id}",
            metadata={"llm_id": llm_id, "stream": True},
        )
        await llm_task_handler.log_info(
            f"[SecurityHandler] 开始流式 LLM 调用 (llm_id={llm_id}, "
            f"msg_count={len(messages)}, tools={len(tool_list) if tool_list else 0})"
        )

        # ── 聚合状态 ──────────────────────────────────────────────
        aggregated_text: str = ""
        parsed_tool_calls: list = []
        stop_reason: str = "stop"
        usage: dict[str, int] = {}
        reasoning = None
        producer: str | None = None
        model_name: str | None = None
        raw_error: str | None = None

        try:
            llm_client = self._get_or_create_llm_client(llm_id)

            async for chunk in llm_client.chat_completion_stream(
                system=system,
                messages=messages,
                tool_list=tool_list,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                **kwargs
            ):
                # 透传 StreamChunk 到上层调用方
                yield chunk

                # 聚合
                if chunk.type == "text_delta" and chunk.text_delta:
                    aggregated_text += chunk.text_delta
                elif chunk.type == "tool_call_end" and chunk.tool_call:
                    parsed_tool_calls.append(chunk.tool_call)
                elif chunk.type == "finish":
                    stop_reason = chunk.stop_reason or "stop"
                    usage = chunk.usage or {}
                    reasoning = chunk.reasoning
                    producer = chunk.produced_by_provider
                    model_name = chunk.produced_by_model

        except Exception as e:
            raw_error = str(e)
            # 🆕 通知上层调用方流已失败，对齐 security_denied 路径
            yield StreamChunk(
                type="fail",
                error_code="llm_stream_error",
                error_message=str(e),
            )

        end_timestamp = datetime.now().isoformat()
        success = raw_error is None

        # ── 记录日志 ──────────────────────────────────────────────
        _log_record = self._save_llm_call_record_v2(
            session_id=session_id,
            llm_id=llm_id,
            messages=self._build_messages_summary(system, messages),
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            security_decision=decision,
            success=success,
            response="[streamed]" if success else "",
            error=raw_error or "",
            tool_list=tool_list,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            kwargs=kwargs,
        )

        await self.security_monitor.after_llm_call(
            session_id=session_id,
            subagent_id=None,
            llm_id=llm_id,
            system=system,
            messages=messages,
            tool_list=tool_list,
            tool_choices=tool_choice,
            kwargs=kwargs,
            success=success,
            stop_reason=stop_reason if success else None,
            error=raw_error,
        )

        # ── Artifact ──────────────────────────────────────────────
        try:
            _llm_reporter = ArtifactReporter.for_task(llm_task_handler.get_task_id())
            payload: dict[str, Any] = {
                "success": success,
                "error": raw_error,
                "security_decision": decision,
                "llm_id": llm_id,
                "stream": True,
                "text": aggregated_text,
                "stop_reason": stop_reason,
                "usage": usage,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in parsed_tool_calls
                ],
                "produced_by_provider": producer,
                "produced_by_model": model_name,
            }
            if _log_record is not None:
                payload["record_log"] = _log_record
            _safe_attach(_llm_reporter, "llm_response", payload)
            if _log_record is not None:
                _safe_attach(_llm_reporter, "meta", "record_log", _log_record)
        except Exception as _e:
            _rlog.debug("core_service", f"[SecurityHandler] artifact attach (stream) skipped: {_e}")

        # ── 任务日志 ──────────────────────────────────────────────
        if success:
            tool_names = [tc.name for tc in parsed_tool_calls]
            resp_preview = aggregated_text[:50]
            await llm_task_handler.log_info(
                f"[SecurityHandler] 流式LLM调用成功 | "
                f"tokens={{prompt:{usage.get('prompt_tokens','?')}, "
                f"completion:{usage.get('completion_tokens','?')}}} | "
                f"stop={stop_reason} | "
                f"response=\"{resp_preview}\" | tools={tool_names}"
            )
            await llm_task_handler.complete({
                "success": True,
                "response": aggregated_text,
                "usage": usage,
                "security_decision": decision,
                "error": None,
            })
        else:
            await llm_task_handler.log_error(
                f"[SecurityHandler] 流式LLM调用失败: {raw_error}"
            )
            await llm_task_handler.fail(error=raw_error or "Unknown error")

        # ── 返回聚合结果 ──────────────────────────────────────────
        # 聚合数据已通过 finish chunk 发送，generator 结束即可。
        # 调用方从 chunks 中重建 result dict：检查 type="finish" chunk 的
        # stop_reason 是否为 "error" 来判断成功/失败。
        return

    async def request_tool_call(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        subagent_label: str | None = None,
    ) -> dict[str, Any]:
        """申请工具调用
        
        Args:
            session_id: 会话 ID
            tool_name: 工具名称
            tool_args: 工具参数
            subagent_label: 有值时表示调用者是子 Agent（用于提示用户哪个
                            subagent 发起了当前 query）。为 None 时视为主 Agent。
            
        Returns:
            执行结果 {
                "success": bool,
                "output": str | None,
                "error": str | None,
                "security_decision": str
            }
        """
        # 安全审查
        review_result = await self.security_monitor.before_tool_call(
            session_id=session_id,
            subagent_id=None,
            tool_name=tool_name,
            tool_args=tool_args,
        )

        decision = review_result["decision"]

        # 如果被拒绝
        if decision == "deny":
            _rlog.warning("core_service", f"[SecurityHandler] 工具调用被拒绝 (session={session_id}): {tool_name} - {review_result['reason']}")
            await self.security_monitor.after_tool_call(
                session_id=session_id,
                subagent_id=None,
                tool_name=tool_name,
                tool_args=tool_args,
                success=False,
                output=None,
                error=f"Security check failed: {review_result['reason']}",
            )
            return {
                "success": False,
                "output": None,
                "error": f"Security check failed: {review_result['reason']}",
                "security_decision": decision,
                "log_record": None,
            }

        # 如果需要用户确认
        if decision == "query":
            query_spec = review_result.get("query_spec") or {}
            raw_choices = query_spec.get("choices") or [
                {"value": "allow", "label": "Allow"},
                {"value": "deny",  "label": "Deny"},
            ]
            choices = [
                Choice(value=c["value"], label=c.get("label", c["value"]),
                       description=c.get("description", ""))
                for c in raw_choices
            ]
            raw_prompt = query_spec.get("prompt", "Allow this tool call?")
            # Simple template substitution for tool_args fields
            prompt = raw_prompt
            for k, v in (tool_args or {}).items():
                prompt = prompt.replace(f"{{tool_args.{k}}}", str(v))

            # 加上调用者身份前缀，让用户能区分哪个 (sub)agent 要执行这个危险动作
            _label = subagent_label or "主 Agent"
            prompt = f"[安全确认: {_label} 调用 {tool_name}] {prompt}"

            pending = PendingQuery(
                query_id=str(__import__("uuid").uuid4()),
                session_id=session_id,
                origin="rule",
                tool_name=tool_name,
                tool_args=tool_args,
                prompt=prompt,
                choices=choices,
                deny_values=query_spec.get("deny_values") or ["deny"],
                default=query_spec.get("default"),
                timeout_s=query_spec.get("timeout_s"),
            )
            qs = get_query_service()
            query_id = await qs.enqueue(pending)
            _rlog.info(
                "core_service",
                f"[SecurityHandler] awaiting user query query_id={query_id} "
                f"session={session_id} tool={tool_name}",
            )
            chosen = await qs.wait_resolved(query_id)
            _rlog.info(
                "core_service",
                f"[SecurityHandler] query resolved query_id={query_id} "
                f"chosen={chosen!r}",
            )
            if chosen == CANCEL_SENTINEL or chosen in pending.deny_values:
                deny_reason = (
                    "user_cancelled" if chosen == CANCEL_SENTINEL else f"user_denied ({chosen})"
                )
                await self.security_monitor.after_tool_call(
                    session_id=session_id,
                    subagent_id=None,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    success=False,
                    output=None,
                    error=deny_reason,
                )
                return {
                    "success": False,
                    "output": None,
                    "error": deny_reason,
                    "security_decision": "deny",
                    "log_record": None,
                }
            # User approved — continue as ALLOW
            decision = "allow"

        # 如果有警告
        if decision == "warn":
            _rlog.warning("core_service", f"[SecurityHandler] 工具调用警告 (session={session_id}): {tool_name} - {review_result['reason']}")
        
        # 记录开始时间戳
        start_timestamp = datetime.now().isoformat()
        
        # 【2026年04月03日21:22:00新增】路径占位符解析
        # 【PathResolver 已移除】tool_args 直接透传，不再扫描 / 解析 {{...}} 占位符
        resolved_tool_args = tool_args.copy()

        # Inject _session_id for tools that need it (query_user and any future
        # tools that accept a _session_id kwarg).  Standard tools use **kwargs
        # and ignore unknown keys, so this is broadly safe; callers that pop it
        # explicitly will benefit.
        resolved_tool_args["_session_id"] = session_id
        # Inject _subagent_label so query_user can prefix its prompt with the
        # caller identity (helps the user disambiguate concurrent queries).
        if subagent_label is not None:
            resolved_tool_args["_subagent_label"] = subagent_label

        # 执行实际的工具调用
        try:
            # 获取 ToolManager
            from ..tool import get_tool_manager
            tool_manager = get_tool_manager()
            
            # 执行工具
            tool_result = await tool_manager.execute_tool(tool_name, **resolved_tool_args)
            
            # 记录结束时间戳
            end_timestamp = datetime.now().isoformat()
            
            # 转换 ToolResult 为字典格式
            if tool_result.is_success():
                result_dict = {
                    "success": True,
                    "output": tool_result.output,
                    "error": None,
                    "security_decision": decision,
                    "content_parts": tool_result.content_parts,
                }

                # 保存工具调用记录（成功）
                _log_record = self._save_tool_call_record(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=resolved_tool_args,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    security_decision=decision,
                    success=True,
                    output=tool_result.output or "",
                    error="",
                    content_parts=tool_result.content_parts,
                )
                result_dict["log_record"] = _log_record

                await self.security_monitor.after_tool_call(
                    session_id=session_id,
                    subagent_id=None,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    success=True,
                    output=str(tool_result.output or ""),
                    error=None,
                )
                return result_dict
            else:
                result_dict = {
                    "success": False,
                    "output": None,
                    "error": tool_result.error or f"Tool execution failed: {tool_result.status.value}",
                    "security_decision": decision
                }

                # 保存工具调用记录（失败）
                _log_record = self._save_tool_call_record(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    security_decision=decision,
                    success=False,
                    output="",
                    error=tool_result.error or f"Tool execution failed: {tool_result.status.value}"
                )
                result_dict["log_record"] = _log_record

                await self.security_monitor.after_tool_call(
                    session_id=session_id,
                    subagent_id=None,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    success=False,
                    output=None,
                    error=result_dict["error"],
                )
                return result_dict

        except Exception as e:
            # 记录结束时间戳
            end_timestamp = datetime.now().isoformat()

            _rlog.error(
                "core_service",
                f"[SecurityHandler] 工具执行异常 (session={session_id}): "
                f"{tool_name} - {e!s}\n{traceback.format_exc()}",
            )

            # 保存工具调用记录（异常）
            _log_record = self._save_tool_call_record(
                session_id=session_id,
                tool_name=tool_name,
                tool_args=tool_args,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                security_decision=decision,
                success=False,
                output="",
                error=str(e)
            )

            await self.security_monitor.after_tool_call(
                session_id=session_id,
                subagent_id=None,
                tool_name=tool_name,
                tool_args=tool_args,
                success=False,
                output=None,
                error=str(e),
            )
            return {
                "success": False,
                "output": None,
                "error": str(e),
                "security_decision": decision,
                "log_record": _log_record,
            }

    async def request_tool_call_v2(
        self,
        loop_task_handler: SessionTaskHandlerV2,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        """申请工具调用（V2 版，自动创建 TOOL_EXECUTION 子任务并上报工件）。

        与 request_tool_call 的区别：
        - 接收一个父级 task_handler（通常是 agent loop 的 handler），
          内部为每次工具调用创建 TaskType.TOOL_EXECUTION 子任务，
          自动 start/complete/fail。
        - 自动把 tool_args / tool_result / error_trace / file_edit 工件
          挂到该子任务上，供 /tasks3 详情面板懒加载。
        - 内部仍然委托现有 request_tool_call 完成安全审查、路径解析、执行。

        Args:
            loop_task_handler: 父任务（通常是 agent loop 任务）的 handler；
                               session_id 从其内部获取。
            tool_name:         工具名
            tool_args:         工具参数

        Returns:
            与 request_tool_call 相同结构的字典。
        """
        session_id = loop_task_handler.get_session_id()
        # Subagent identity (if any) so downstream query prompts can be tagged.
        subagent_label: str | None = None
        try:
            sid = loop_task_handler.get_subagent_id()
            if sid:
                subagent_label = f"SubAgent {sid}"
        except Exception:
            subagent_label = None

        # 创建 TOOL_EXECUTION 子任务
        tool_task_handler: SessionTaskHandlerV2 | None = None
        try:
            tool_task_handler = await loop_task_handler.create_subtask(
                task_type=TaskType.TOOL_EXECUTION,
                name=f"Tool Call: {tool_name}",
                description=f"工具调用 {tool_name}",
                metadata={
                    "tool_name": tool_name,
                    "args_preview": str(tool_args)[:200],
                },
            )
            await tool_task_handler.start()
        except Exception as e:
            _rlog.warning("core_service", f"[SecurityHandler] 创建 TOOL_EXECUTION 子任务失败: {e}")

        # 工件上报：参数
        reporter: ArtifactReporter | None = None
        if tool_task_handler is not None:
            try:
                reporter = ArtifactReporter.for_task(tool_task_handler.get_task_id())
            except Exception:
                reporter = None
            _safe_attach(reporter, "tool_args", tool_name, tool_args or {})

        # 委托现有实现
        result = await self.request_tool_call(
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            subagent_label=subagent_label,
        )

        # 工件上报：结果 / 错误堆栈 / 文件副作用
        success = bool(result.get("success"))
        if reporter is not None:
            content_for_artifact = result.get("output") if success else (result.get("error") or "")
            _safe_attach(reporter, "tool_result", tool_name, content_for_artifact, success=success)

            log_record = result.get("log_record")
            if log_record is not None:
                _safe_attach(reporter, "meta", "record_log", log_record)

            if not success and result.get("error"):
                _safe_attach(reporter, "error_trace", str(result["error"]), name=f"error[{tool_name}]")

            # KIND_FILE_EDIT：成功且命中白名单时记录被改动的文件
            if success and tool_name in _FILE_EDIT_TOOLS:
                arg_key = _FILE_EDIT_TOOLS[tool_name]
                file_path = (tool_args or {}).get(arg_key)
                if isinstance(file_path, str) and file_path:
                    _safe_attach(
                        reporter, "file_edit", file_path,
                        {"tool": tool_name, "args": tool_args},
                    )

        # 收尾：标记子任务完成 / 失败
        if tool_task_handler is not None:
            try:
                if success:
                    await tool_task_handler.complete(result={"output_preview": str(result.get("output") or "")[:200]})
                else:
                    await tool_task_handler.fail(error=str(result.get("error") or "tool failed"))
            except Exception as e:
                _rlog.debug("core_service", f"[SecurityHandler] tool 子任务收尾失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 子 Agent 专属接口
    # ------------------------------------------------------------------

    async def request_subagent_llm_call(
        self,
        session_id: str,
        subagent_id: str,
        llm_id: str,
        system: str | None,
        messages: list[UnifiedMessage],
        task_handler: SessionTaskHandlerV2,
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """子 Agent 内部的 LLM 调用接口

        与 request_llm_call_v2 对称，额外接受 subagent_id，
        日志路径写入 llm_calls/{session_id}/subagents/{subagent_id}/ 目录。

        Returns:
            与 request_llm_call_v2 相同格式的字典
        """
        llm_task_handler = await task_handler.create_subtask(
            task_type=TaskType.LLM_CALL,
            name=(
                f"Session {session_id} SubAgent {subagent_id} LLM Call: "
                f"llm_id={llm_id}, "
                f"num_messages={len(messages)}, "
                f"tools={len(tool_list) if tool_list else 0}"
            ),
            description=f"子 Agent {subagent_id} LLM 调用 (LLM ID: {llm_id})",
            metadata={
                "subagent_id": subagent_id,
                "llm_id": llm_id,
            }
        )

        # 记录开始
        await llm_task_handler.log_info(
            f"[SecurityHandler] 开始 子Agent LLM 调用 (subagent_id={subagent_id}, llm_id={llm_id}, "
            f"msg_count={len(messages)}, tools={len(tool_list) if tool_list else 0})"
        )

        # 安全审查
        review_result = await self.security_monitor.before_llm_call(
            session_id=session_id,
            subagent_id=subagent_id,
            llm_id=llm_id,
            system=system,
            messages=messages,
            tool_list=tool_list,
            tool_choices=tool_choice,
            kwargs=kwargs,
        )
        decision = review_result["decision"]

        if decision == "deny":
            await llm_task_handler.log_warning(
                f"[SecurityHandler] 子Agent LLM调用被拒绝 (subagent_id={subagent_id}, llm_id={llm_id}): {review_result['reason']}",
            )
            err_msg = f"Security check failed: {review_result['reason']}"
            await self.security_monitor.after_llm_call(
                session_id=session_id,
                subagent_id=subagent_id,
                llm_id=llm_id,
                system=system,
                messages=messages,
                tool_list=tool_list,
                tool_choices=tool_choice,
                kwargs=kwargs,
                success=False,
                stop_reason=None,
                error=err_msg,
            )
            await llm_task_handler.fail(error=err_msg)
            return {
                "success": False,
                "v2_response": None,
                "response": None,
                "usage": {},
                "security_decision": decision,
                "error": err_msg,
            }

        start_timestamp = datetime.now().isoformat()

        try:
            llm_client = self._get_or_create_llm_client(llm_id)

            v2_resp: ChatResponseV2 = await llm_client.chat_completion_v2(
                system=system,
                messages=messages,
                tool_list=tool_list,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                **kwargs
            )

            end_timestamp = datetime.now().isoformat()

            # 日志路径：llm_calls/{session_id}/subagents/{subagent_id}/
            _log_record = self._save_subagent_llm_record(
                session_id=session_id,
                subagent_id=subagent_id,
                llm_id=llm_id,
                messages=self._build_messages_summary(system, messages),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                security_decision=decision,
                success=True,
                response=v2_resp.raw_response,
                tool_list=tool_list,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                kwargs=kwargs,
            )

            await self.security_monitor.after_llm_call(
                session_id=session_id,
                subagent_id=subagent_id,
                llm_id=llm_id,
                system=system,
                messages=messages,
                tool_list=tool_list,
                tool_choices=tool_choice,
                kwargs=kwargs,
                success=True,
                stop_reason=v2_resp.stop_reason,
                error=None,
            )
            usage = v2_resp.usage or {}
            tool_names = [tc.name for tc in (v2_resp.tool_calls or [])]
            resp_preview = (v2_resp.text or "")[:200]
            await llm_task_handler.log_info(
                f"[SecurityHandler] 子Agent LLM调用成功 (subagent_id={subagent_id}) | "
                f"tokens={{prompt:{usage.get('prompt_tokens','?')}, "
                f"completion:{usage.get('completion_tokens','?')}}} | "
                f"stop={v2_resp.stop_reason} | "
                f"response=\"{resp_preview}\" | tools={tool_names}"
            )
            await llm_task_handler.complete(result={
                "success": True,
                "response": v2_resp.text,
                "tool_calls": [tc.to_dict() for tc in v2_resp.tool_calls] if v2_resp.tool_calls else None,
                "usage": v2_resp.usage,
                "security_decision": decision,
                "error": None,
            })

            # 工件上报：完整 LLM 响应（懒加载用）
            try:
                _safe_attach(
                    ArtifactReporter.for_task(llm_task_handler.get_task_id()),
                    "llm_response",
                    {
                        "success": True,
                        "llm_id": llm_id,
                        "subagent_id": subagent_id,
                        "raw_response": self._serialize_llm_response(v2_resp.raw_response),
                        "text": v2_resp.text,
                        "stop_reason": v2_resp.stop_reason,
                        "usage": v2_resp.usage,
                        "tool_calls": [
                            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                            for tc in (v2_resp.tool_calls or [])
                        ],
                        "produced_by_provider": v2_resp.produced_by_provider,
                        "produced_by_model": v2_resp.produced_by_model,
                        "record_log": _log_record,
                    },
                )
                if _log_record is not None:
                    _safe_attach(
                        ArtifactReporter.for_task(llm_task_handler.get_task_id()),
                        "meta", "record_log", _log_record,
                    )
            except Exception as _e:
                _rlog.debug("core_service", f"[SecurityHandler] artifact attach (subagent llm_response) skipped: {_e}")

            return {
                "success": True,
                "v2_response": v2_resp,
                "response": v2_resp.text,
                "usage": v2_resp.usage,
                "security_decision": decision,
                "error": None,
            }

        except Exception as e:
            end_timestamp = datetime.now().isoformat()
            _log_record = self._save_subagent_llm_record(
                session_id=session_id,
                subagent_id=subagent_id,
                llm_id=llm_id,
                messages=self._build_messages_summary(system, messages),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                security_decision=decision,
                success=False,
                error=str(e),
            )
            await self.security_monitor.after_llm_call(
                session_id=session_id,
                subagent_id=subagent_id,
                llm_id=llm_id,
                system=system,
                messages=messages,
                tool_list=tool_list,
                tool_choices=tool_choice,
                kwargs=kwargs,
                success=False,
                stop_reason=None,
                error=str(e),
            )
            await llm_task_handler.log_error(
                f"[SecurityHandler] 子Agent LLM调用失败 "
                f"(subagent_id={subagent_id}): {e}\n{traceback.format_exc()}",
            )
            await llm_task_handler.fail(error=str(e))
            try:
                _safe_attach(
                    ArtifactReporter.for_task(llm_task_handler.get_task_id()),
                    "llm_response",
                    {
                        "success": False,
                        "llm_id": llm_id,
                        "subagent_id": subagent_id,
                        "error": str(e),
                        "record_log": _log_record,
                    },
                )
                if _log_record is not None:
                    _safe_attach(
                        ArtifactReporter.for_task(llm_task_handler.get_task_id()),
                        "meta", "record_log", _log_record,
                    )
                _safe_attach(
                    ArtifactReporter.for_task(llm_task_handler.get_task_id()),
                    "error_trace", traceback.format_exc(),
                    name=f"error[llm:{llm_id}]",
                )
            except Exception:
                pass
            return {
                "success": False,
                "v2_response": None,
                "response": None,
                "usage": {},
                "security_decision": decision,
                "error": str(e),
            }

    async def request_subagent_tool_call(
        self,
        session_id: str,
        subagent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        task_handler: SessionTaskHandlerV2,
    ) -> dict[str, Any]:
        """子 Agent 内部的工具调用接口

        与 request_tool_call 对称，额外接受 subagent_id，
        日志路径写入 tool_calls/{session_id}/subagents/{subagent_id}/ 目录。
        经由 SecurityMonitor 安全审查，支持路径占位符解析。

        Returns:
            {"success": bool, "output": Any, "error": str|None, "security_decision": str}
        """
        tool_task_handler = await task_handler.create_subtask(
            task_type=TaskType.TOOL_EXECUTION,
            name=(
                f"Session {session_id} SubAgent {subagent_id} Tool Call: "
                f"tool_name={tool_name}, "
                + ", ".join([f"{k}={str(v)[:50]}" for k,v in tool_args.items()])
            ),
            description=f"子 Agent {subagent_id} 发起的工具调用 (工具名称: {tool_name})",
            metadata={
                "subagent_id": subagent_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
        )

        # 安全审查
        review_result = await self.security_monitor.before_tool_call(
            session_id=session_id,
            subagent_id=subagent_id,
            tool_name=tool_name,
            tool_args=tool_args,
        )
        decision = review_result["decision"]

        if decision == "deny":
            await tool_task_handler.log_warning(
                f"[SecurityHandler] 子Agent工具调用被拒绝 "
                f"(subagent_id={subagent_id}, tool={tool_name}): {review_result['reason']}",
            )
            err_msg = f"Security check failed: {review_result['reason']}"
            await self.security_monitor.after_tool_call(
                session_id=session_id,
                subagent_id=subagent_id,
                tool_name=tool_name,
                tool_args=tool_args,
                success=False,
                output=None,
                error=err_msg,
            )
            await tool_task_handler.fail(error=err_msg)
            return {
                "success": False,
                "output": None,
                "error": err_msg,
                "security_decision": decision,
            }

        if decision == "warn":
            await tool_task_handler.log_warning(
                f"[SecurityHandler] 子Agent工具调用警告 "
                f"(subagent_id={subagent_id}, tool={tool_name}): {review_result['reason']}",
            )

        # 【PathResolver 已移除】tool_args 直接透传，不再解析路径占位符
        resolved_tool_args = tool_args.copy()
        resolved_tool_args["_session_id"] = session_id
        # Tag query_user prompts with the calling subagent (for disambiguation
        # when multiple subagents run concurrently).
        resolved_tool_args["_subagent_label"] = f"SubAgent {subagent_id}"

        # 工件上报：参数（指针解析后）
        try:
            _sub_reporter = ArtifactReporter.for_task(tool_task_handler.get_task_id())
        except Exception:
            _sub_reporter = None
        _safe_attach(_sub_reporter, "tool_args", tool_name, resolved_tool_args or {})

        start_timestamp = datetime.now().isoformat()

        try:
            from ..tool import get_tool_manager
            tool_manager = get_tool_manager()
            tool_result = await tool_manager.execute_tool(tool_name, **resolved_tool_args)
            end_timestamp = datetime.now().isoformat()

            if tool_result.is_success():
                _log_record = self._save_subagent_tool_record(
                    session_id=session_id,
                    subagent_id=subagent_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    security_decision=decision,
                    success=True,
                    output=str(tool_result.output or ""),
                    content_parts=tool_result.content_parts,
                )
                await self.security_monitor.after_tool_call(
                    session_id=session_id,
                    subagent_id=subagent_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    success=True,
                    output=str(tool_result.output or ""),
                    error=None,
                )
                await tool_task_handler.log_info(
                    f"[SecurityHandler] 子Agent工具调用成功 (subagent_id={subagent_id}, tool={tool_name})"
                )
                result_state = {
                    "success": True,
                    "output": tool_result.output,
                    "error": None,
                    "security_decision": decision,
                    "content_parts": tool_result.content_parts,
                }
                _safe_attach(_sub_reporter, "tool_result", tool_name,
                             tool_result.output if isinstance(tool_result.output, (str, dict, list)) else str(tool_result.output or ""),
                             success=True)
                if _log_record is not None:
                    _safe_attach(_sub_reporter, "meta", "record_log", _log_record)
                if tool_name in _FILE_EDIT_TOOLS:
                    _arg_key = _FILE_EDIT_TOOLS[tool_name]
                    _fp = (resolved_tool_args or {}).get(_arg_key)
                    if isinstance(_fp, str) and _fp:
                        _safe_attach(_sub_reporter, "file_edit", _fp,
                                     {"tool": tool_name, "args": resolved_tool_args})
                await tool_task_handler.complete(result=result_state)
                return result_state
            else:
                error_msg = tool_result.error or f"Tool execution failed: {tool_result.status.value}"
                _log_record = self._save_subagent_tool_record(
                    session_id=session_id,
                    subagent_id=subagent_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    security_decision=decision,
                    success=False,
                    error=error_msg,
                )
                await self.security_monitor.after_tool_call(
                    session_id=session_id,
                    subagent_id=subagent_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    success=False,
                    output=None,
                    error=error_msg,
                )
                await tool_task_handler.log_error(
                    f"[SecurityHandler] 子Agent工具执行失败 (subagent_id={subagent_id}, tool={tool_name}): {error_msg}"
                )
                await tool_task_handler.fail(error=error_msg)
                _safe_attach(_sub_reporter, "tool_result", tool_name, error_msg, success=False)
                if _log_record is not None:
                    _safe_attach(_sub_reporter, "meta", "record_log", _log_record)
                return {
                    "success": False,
                    "output": None,
                    "error": error_msg,
                    "security_decision": decision,
                }

        except Exception as e:
            end_timestamp = datetime.now().isoformat()
            _log_record = self._save_subagent_tool_record(
                session_id=session_id,
                subagent_id=subagent_id,
                tool_name=tool_name,
                tool_args=tool_args,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                security_decision=decision,
                success=False,
                error=str(e),
            )
            await self.security_monitor.after_tool_call(
                session_id=session_id,
                subagent_id=subagent_id,
                tool_name=tool_name,
                tool_args=tool_args,
                success=False,
                output=None,
                error=str(e),
            )
            await tool_task_handler.log_error(
                f"[SecurityHandler] 子Agent工具调用异常 (subagent_id={subagent_id}, tool={tool_name}): {e}\n{traceback.format_exc()}",
            )
            await tool_task_handler.fail(error=str(e))
            _safe_attach(_sub_reporter, "tool_result", tool_name, str(e), success=False)
            if _log_record is not None:
                _safe_attach(_sub_reporter, "meta", "record_log", _log_record)
            _safe_attach(_sub_reporter, "error_trace", traceback.format_exc(),
                         name=f"error[{tool_name}]")
            return {
                "success": False,
                "output": None,
                "error": str(e),
                "security_decision": decision,
            }

    if TYPE_CHECKING:
        from ..context import BaseSubAgentContextHandler
    async def request_subagent_call(
        self,
        session_id: str,
        subagent_id: str,
        subagent_type: str,
        subagent_handler: Callable,
        workspace_path: "Path",
        base_config: dict[str, Any],
        context_handler: "BaseSubAgentContextHandler",
        user_message: dict[str, Any],
        subagent_task_handler: SessionTaskHandlerV2,
        stream_callback: Any = None,
    ) -> dict[str, Any]:
        """子 Agent 创建与执行的统一安全通道

        与 request_tool_call 对称，可在此处添加子 Agent 专属安全规则（当前先 allow）。
        日志记录时附带 subagent_id 字段。

        Args:
            session_id:            父会话 ID
            subagent_id:           子 Agent 唯一标识
            subagent_type:         子 Agent 类型（SUBAGENT_REGISTRY 的键）
            subagent_handler:      AgentFactory.create_subagent 的引用
            workspace_path:        子 Agent 独立工作目录（已创建）
            base_config:           主 Agent 配置（含 llm 等字段）
            context_handler:       BaseSubAgentContextHandler 实例（已初始化）
            user_message:          任务消息 dict
            subagent_task_handler: 任务进度通知回调

        Returns:
            {"success": bool, "output": str, "error": str|None, "security_decision": str}
        """
        decision = "allow"
        log_tag = f"session_{session_id}"

        await subagent_task_handler.log_info(
            f"[SecurityHandler] 开始执行子Agent (subagent_id={subagent_id}, type={subagent_type})",
        )

        try:
            # 使用 AgentFactory.create_subagent() 创建 BaseSubAgent 实例
            agent = subagent_handler(
                subagent_type=subagent_type,
                session_id=session_id,
                subagent_id=subagent_id,
                workspace_path=workspace_path,
                base_config=base_config,
            )

            # 驱动子 Agent 执行
            result_content: str = await agent.process(
                user_message=user_message,
                context_handler=context_handler,
                subagent_task_handler=subagent_task_handler,
                stream_callback=stream_callback,
            )

            _rlog.info(
                log_tag,
                f"[SecurityHandler] 子Agent执行完成 (subagent_id={subagent_id}, output_len={len(result_content)})",
            )

            # ── 子 Agent 流完成后：发送 done:True + 保存 Artifact ──
            if stream_callback is not None and hasattr(stream_callback, '_finalize'):
                try:
                    await stream_callback._finalize(
                        agent_task_id=subagent_task_handler.get_task_id(),
                    )
                except Exception:
                    pass

            return {
                "success": True,
                "output": result_content,
                "error": None,
                "security_decision": decision,
            }

        except Exception as e:
            import traceback
            await subagent_task_handler.log_error(
                f"[SecurityHandler] 子Agent执行异常 (subagent_id={subagent_id}): {e}\n{traceback.format_exc()}",
            )
            return {
                "success": False,
                "output": "",
                "error": str(e),
                "security_decision": decision,
            }

    # ------------------------------------------------------------------
    # 子 Agent 日志记录辅助方法
    # ------------------------------------------------------------------

    def _save_subagent_llm_record(
        self,
        session_id: str,
        subagent_id: str,
        llm_id: str,
        messages: list[dict[str, Any]],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        response: Any = "",
        error: str = "",
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict | None:
        """Delegate to RecordStore.write_subagent_llm_call."""
        return self.record_store.write_subagent_llm_call(
            session_id=session_id,
            subagent_id=subagent_id,
            llm_id=llm_id,
            messages=messages,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            security_decision=security_decision,
            success=success,
            response=response,
            error=error,
            tool_list=tool_list,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            kwargs=kwargs,
        )

    def _save_subagent_tool_record(
        self,
        session_id: str,
        subagent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        start_timestamp: str,
        end_timestamp: str,
        security_decision: str,
        success: bool,
        output: str = "",
        error: str = "",
        content_parts: list | None = None,
    ) -> dict | None:
        """Delegate to RecordStore.write_subagent_tool_call."""
        return self.record_store.write_subagent_tool_call(
            session_id=session_id,
            subagent_id=subagent_id,
            tool_name=tool_name,
            tool_args=tool_args,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            security_decision=security_decision,
            success=success,
            output=output,
            error=error,
            content_parts=content_parts,
        )
