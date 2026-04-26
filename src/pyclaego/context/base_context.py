"""Context 基类 - 定义上下文处理器的抽象接口"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Any, List, Optional, Union, TYPE_CHECKING
from pathlib import Path

from .history_manager import HistoryFileManager, HistoryFormat

if TYPE_CHECKING:
    from ..llm import UnifiedMessage, ToolDefinition, ToolCall, ToolCallResult, ReasoningArtifact
    from ..task_manager import SessionTaskHandlerV2


BASE_SYSTEM_PROMPT = """# Identity
你是 PyClaw-CC 的智能助手。
你的职责是理解用户的需求，提供准确、有帮助的回答。
请保持回答简洁、专业，并尽可能提供具体的建议。

# Security
如果需要保存文件，请在工作目录下进行操作。

使用工具指定路径时，必须包含路径占位符：
* `{{WORKSPACE}}`表示工作目录。
* `{{SESSION_SKILL_ROOT}}`表示技能所在目录。

所有路径均用占位符开头，可以在占位符之后衔接子路径。
**禁止**指定具体的目录（如`/`, `~`, `.`），这是危险的操作！
"""
# 【2026年04月08日13:24:51新增】取消路径脱敏
BASE_SYSTEM_PROMPT = """# Identity
你是 PyClaw-CC 的智能助手。
你的职责是理解用户的需求，提供准确、有帮助的回答。
请保持回答简洁、专业，并尽可能提供具体的建议。

# Security
如果需要保存或修改文件，请在工作目录下进行操作。
* 工作目录：`{wordspace_root}`
* 技能目录：`{wordspace_root}/skills`
"""


class ContextCheckPoint(str, Enum):
    """ContextHandler.get_llm_context 调用时机枚举
    
    标识在不同阶段调用 get_llm_context 的场景，各实现类根据时机
    执行不同的上下文构建和消息暂存逻辑。
    
    继承 str 以确保枚举值可以直接与字符串比较，保证向后兼容性。
    """
    
    # ── 主流程时机 ──────────────────────────────────────
    BEFORE_LOOP = "before_loop"
    """循环开始前（用户消息进入时），暂存 new_message 并返回完整上下文"""
    
    BEFORE_LLM_CALL = "before_llm_call"
    """LLM 调用前（默认值），行为通常与 BEFORE_LOOP 一致"""
    
    AFTER_LLM_CALL = "after_llm_call"
    """LLM 调用后，用于记忆工具拦截、THINK 阶段通知等后处理"""
    
    AFTER_TOOL_CALL = "after_tool_call"
    """工具执行后，暂存工具调用轮消息（assistant + user）"""
    
    AFTER_LOOP = "after_loop"
    """整轮 process 结束，暂存 assistant 最终回复并批量写盘"""
    
    # ── 特殊时机 ──────────────────────────────────────
    COMPRESS = "compress"
    """上下文压缩时机（预留，当前未大量使用）"""
    
    RAG_RETRIEVE = "rag_retrieve"
    """RAG 检索时机（预留，当前未使用）"""
    
    SPAWN_CONTEXT_INIT = "spawn_context_init"
    """SpawnAgent 在调度子 Agent 前触发，用于获取父 Agent 上下文快照"""


class BaseContextHandler(ABC):
    """上下文处理器抽象基类
    
    所有上下文处理器必须继承此类并实现核心方法
    """
    
    def __init__(self, session_id: str, workspace_path: Path, config: Dict[str, Any]):
        """初始化上下文处理器
        
        Args:
            session_id: 会话 ID
            workspace_path: 工作空间路径
            config: 上下文配置字典
        """
        self.session_id = session_id
        self.workspace_path = workspace_path
        self.config = config
        self.context_type = config.get("type", "unknown")
    
    @abstractmethod
    async def get_llm_context(
        self, 
        check_point: Union[ContextCheckPoint, str] = ContextCheckPoint.BEFORE_LLM_CALL,
        include_system_prompt: bool = True,
        **kwargs
    ) -> List[Dict[str, str]]:
        """构建 LLM API 所需的上下文格式（异步方法）
        
        Args:
            check_point: 调用时机标识，推荐使用 ContextCheckPoint 枚举：
                - ContextCheckPoint.BEFORE_LLM_CALL: LLM 调用之前（默认）
                - ContextCheckPoint.AFTER_LLM_CALL: LLM 调用之后（用于后处理）
                - ContextCheckPoint.COMPRESS: 压缩上下文时
                - ContextCheckPoint.RAG_RETRIEVE: RAG 检索时
                - ContextCheckPoint.AFTER_TOOL_CALL: 工具执行后
                仍支持传入字符串以保持向后兼容性
            include_system_prompt: 是否包含系统提示词
            **kwargs: 其他参数
            
        Returns:
            LLM API 格式的消息列表 [{"role": "...", "content": "..."}, ...]
        """
        pass
    
    @abstractmethod
    def get_recent_messages(self, count: int) -> List[Dict[str, Any]]:
        """获取最近的 N 条消息
        
        Args:
            count: 消息数量
            
        Returns:
            最近的消息列表
        """
        pass
    
    def get_system_prompt(self) -> str:
        """获取系统提示词（可被子类覆盖）
        
        Returns:
            系统提示词字符串
        """
        # 从配置中获取，如果没有则使用默认值
        return self.config.get("system_prompt", self._get_default_system_prompt())
    
    def _get_default_system_prompt(self) -> str:
        """获取默认系统提示词
        
        Returns:
            默认系统提示词
        """
        # 【2026年04月08日13:29:47修改】在系统提示词里添加工作目录绝对路径
        return BASE_SYSTEM_PROMPT.format(wordspace_root=self.workspace_path.absolute().as_posix())
    
    def get_info(self) -> Dict[str, Any]:
        """获取上下文处理器信息
        
        Returns:
            信息字典
        """
        return {
            "context_type": self.context_type,
            "session_id": self.session_id,
            "config": self.config
        }


class BaseContextHandlerV2(BaseContextHandler):
    """V2 上下文处理器抽象基类

    继承自 BaseContextHandler，重载 get_llm_context 方法签名：
    返回类型从 List[Dict[str, str]] 改为 Dict[str, Any]，
    格式与 SecurityHandler.request_llm_call_v2 的参数完全对齐：

        {
            "system":    Optional[str],                    # 系统提示词
            "messages":  List[UnifiedMessage],             # 协议无关历史消息列表
            "tool_list": Optional[List[ToolDefinition]],  # 可用工具（None=不使用工具）
        }

    子类必须实现 get_llm_context 和 get_recent_messages。
    SimpleAgent 通过 isinstance(handler, BaseContextHandlerV2) 判断后，
    可直接将返回值解包传入 request_llm_call_v2。

    本基类在 __init__ 中自动创建 HistoryFileManager 实例（self.history_manager），
    供子类直接使用，无需重复实现 history.json / history.jsonl 读写逻辑。

    配置项（config 字典）：
        history_format: 历史文件格式，"json"（默认）| "jsonl" | "auto"
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: Dict[str, Any],
    ) -> None:
        super().__init__(session_id, workspace_path, config)

        # 从配置中读取 history 格式（默认 "auto"，自动检测）
        history_format: HistoryFormat = config.get("history_format", "auto")  # type: ignore[assignment]

        #: 历史消息文件管理器 —— 子类可直接调用其方法读写 history 文件
        self.history_manager = HistoryFileManager(
            workspace_path=workspace_path,
            format=history_format,
            session_id=session_id,
        )

        # context handler 自身的 LLM provider ID（无独立 LLM 调用的子类保持 None）
        self._llm_id: Optional[str] = None

    def get_llm_id(self) -> Optional[str]:
        """获取 context handler 自身使用的 LLM provider ID

        无独立 LLM 调用的 context（如 V5、Window、Simple）返回 None。
        有独立 LLM 调用的子类（如旧版 V3/V4）应在 __init__ 中调用 set_llm_id() 初始化。

        Returns:
            llm_id 字符串，若无独立 LLM 调用则返回 None
        """
        return self._llm_id

    def set_llm_id(self, llm_id: str) -> None:
        """设置 context handler 自身的 LLM provider ID

        Args:
            llm_id: LLM provider ID（对应 config.yaml llm.providers 中的键名）
        """
        self._llm_id = llm_id

    @abstractmethod
    async def get_llm_context(
        self,
        check_point: Union[ContextCheckPoint, str] = ContextCheckPoint.BEFORE_LLM_CALL,
        include_system_prompt: bool = True,
        new_message: Optional[Dict[str, Any]] = None,
        new_messages: Optional[List[Dict[str, Any]]] = None,
        llm_v2_response: Optional[Any] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """构建适配 request_llm_call_v2 的上下文字典（异步方法）

        Args:
            check_point: 调用时机标识，推荐使用 ContextCheckPoint 枚举：
                - ContextCheckPoint.BEFORE_LLM_CALL: LLM 调用之前（默认），同时暂存 new_message（用户消息）
                - ContextCheckPoint.AFTER_LLM_CALL:  LLM 调用之后（ThinkAgent THINK 阶段），仅通知
                - ContextCheckPoint.AFTER_TOOL_CALL: 工具执行后，暂存工具调用轮消息（assistant + user）
                - ContextCheckPoint.AFTER_LOOP:      整轮 process 结束，暂存 assistant 最终回复并**批量写盘**
                - ContextCheckPoint.COMPRESS:        压缩时机
                - ContextCheckPoint.SPAWN_CONTEXT_INIT: SpawnAgent 在调度子 Agent 前触发；
                    默认返回空上下文（子类按需覆盖，提供完整历史快照供 inherit 模式使用）。
                    各子类若需支持 inherit 模式，可覆盖 get_spawn_context_snapshot()
                    或在此时机返回 {"system": ..., "messages": [...], "tool_list": ...}。
                仍支持传入字符串以保持向后兼容性
            include_system_prompt: 是否包含系统提示词（影响 "system" 字段是否为 None）
            new_message:  本次要暂存的单条消息 dict（字段由调用方自定义）
            new_messages: 本次要暂存的多条消息 dict 列表（用于工具调用轮一次追加两条消息）
            llm_v2_response: LLM V2Response 对象（after_llm_call 时传入，供记忆工具拦截）
            **kwargs: 其他透传参数

        消息字段约定（不强制，各 check_point 场景参考）：
            before_llm_call 传入的 user dict：
                {"role": "user", "content": str, "user_id": str, "timestamp": str, "type": "user"}
            after_tool_call 传入的 new_messages 列表（两条）：
                [{"role": "assistant", "content": str, "tool_calls": [...], "type": "tool_call", "timestamp": str},
                 {"role": "user", "content": "", "tool_results": [...], "type": "tool_result", "timestamp": str}]
            after_loop 传入的 assistant dict：
                {"role": "assistant", "content": str, "agent_type": str, "timestamp": str, "type": "assistant"}

        Returns:
            Dict[str, Any]，固定包含以下三个键：
            {
                "system":    Optional[str],
                "messages":  List[UnifiedMessage],
                "tool_list": Optional[List[ToolDefinition]],
            }
            after_llm_call 时机：
            - 有记忆工具调用：{"memory_tool_results": [...], "has_non_memory_calls": bool}
            - 无记忆工具调用：{}（表示 Agent 按正常流程处理）
            非 before_llm_call 时机返回空上下文：
            {"system": None, "messages": [], "tool_list": None}
            spawn_context_init 时机（默认）：
            {"system": None, "messages": [], "tool_list": None}
        """
        pass

    async def get_spawn_context_snapshot(self) -> Dict[str, Any]:
        """获取供 SpawnAgent spawn_context_init 时机使用的上下文快照

        默认实现返回空快照（等同于子 Agent memory_mode="empty" 时的行为），
        行为安全且可预期——未覆盖的 Context 子类在 inherit 模式下等同于 empty 模式。

        子类按需覆盖此方法，返回截至当前时刻的完整历史（system + messages + tool_list），
        供 SpawnSubagentTool 处理后传给子 Agent 作为初始上下文。

        Returns:
            {"system": Optional[str], "messages": List[UnifiedMessage], "tool_list": Optional[List[ToolDefinition]]}
        """
        return {"system": None, "messages": [], "tool_list": None}


class BaseContextHandlerV3(BaseContextHandlerV2):
    """V3 上下文处理器抽象基类
    
    提供基于显式生命周期方法的上下文处理接口，废弃 get_llm_context 方法。
    
    生命周期流程：
    1. handle_before_loop: 对话开始前，返回 LLM 上下文
    2. handle_after_llm_call: LLM 调用后，暂存 assistant 消息
    3. handle_memory_tool_calls: 处理记忆工具调用（如有）
    4. handle_after_tool_calls: 工具调用后，暂存工具结果
    5. handle_after_loop: 对话结束，批量写盘
    
    子类必须实现所有抽象方法。
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        config: Dict[str, Any],
        session_task_handler: Optional["SessionTaskHandlerV2"] = None,
    ) -> None:
        super().__init__(session_id, workspace_path, config)
        self._session_task_handler = session_task_handler

    def set_session_task_handler(self, task_handler: "SessionTaskHandlerV2"):
        """设置会话任务处理器"""
        self._session_task_handler = task_handler

    async def get_llm_context(self, **kwargs):
        """已废弃：V3 使用 handle_* 方法替代此接口"""
        raise NotImplementedError(
            "BaseContextHandlerV3.get_llm_context is deprecated. "
            "Use handle_before_loop() to get LLM context instead."
        )

    @abstractmethod
    async def handle_before_loop(self, user_msg: Dict[str, Any]) -> Dict[str, Any]:
        """在每轮对话开始前准备上下文
        
        Args:
            user_msg: 用户消息 dict，格式:
                {
                    "role": "user",
                    "content": str,
                    "timestamp": str,
                    ...
                }
        
        Returns:
            LLM 上下文字典:
            {
                "system": Optional[str],           # 系统提示词
                "messages": List[UnifiedMessage],  # 历史消息
                "tool_list": Optional[List[ToolDefinition]],  # 工具列表
            }
        
        注意：
        - 必须为 user_msg 分配 group_id（如使用分组机制）
        - 应暂存 user_msg 以便后续写盘
        - 通过 self._session_task_handler.log_info() 记录日志
        """
        raise NotImplementedError("Subclass must implement handle_before_loop()")

    @abstractmethod
    async def handle_after_llm_call(
        self,
        text_reply: str = "",
        tool_calls: Optional[List["ToolCall"]] = None,
        reasoning: Optional["ReasoningArtifact"] = None,
        produced_by_provider: Optional[str] = None,
        produced_by_model: Optional[str] = None,
    ) -> List["UnifiedMessage"]:
        """LLM 调用后处理
        
        Args:
            text_reply: LLM 文本回复
            tool_calls: 工具调用列表（如有）
            reasoning: provider 思考产物（多态 ReasoningArtifact，必须原样回传）
        
        Returns:
            更新后的 UnifiedMessage 列表（已追加本次 assistant 消息）
        
        注意：
        - 应构建并暂存 assistant 消息
        - assistant 消息应继承当前 group_id
        - 如有 tool_calls，应附加到消息中
        - **返回更新后的完整 messages 列表**
        """
        raise NotImplementedError("Subclass must implement handle_after_llm_call()")

    @abstractmethod
    async def handle_memory_tool_calls(
        self,
        tool_calls: List["ToolCall"],
        loop_task_handler: "SessionTaskHandlerV2",
    ) -> Optional[Dict[str, Any]]:
        """处理记忆相关的工具调用
        
        Args:
            tool_calls: 所有工具调用列表
            loop_task_handler: 当前循环的任务处理器
        
        Returns:
            处理结果字典（如不处理记忆工具，返回 None 或所有工具作为非记忆工具）:
            {
                "memory_tool_results": List[ToolCallResult],  # 记忆工具执行结果
                "non_memory_calls": List[ToolCall],           # 非记忆工具调用
            }
        
        注意：
        - 记忆工具应在此方法内执行完成
        - 非记忆工具由 Agent 后续执行
        - 可通过 loop_task_handler.log_info() 记录日志
        """
        raise NotImplementedError("Subclass must implement handle_memory_tool_calls()")

    @abstractmethod
    async def handle_after_tool_calls(
        self,
        tool_results: List["ToolCallResult"],
        last_call_prompt: Optional[str] = None,
        loop_task_handler: Optional["SessionTaskHandlerV2"] = None,
    ) -> List["UnifiedMessage"]:
        """工具调用结束后处理
        
        Args:
            tool_results: 所有工具调用结果（含记忆和非记忆工具）
            last_call_prompt: 可选。最后一轮时传入，会附加到工具结果消息的 content 中，
                提示 LLM 不再调用工具、直接给出最终答案。
        
        Returns:
            更新后的 UnifiedMessage 列表（已追加本次 tool_result 消息）
        
        注意：
        - 应构建并暂存包含工具结果的 user 消息
        - user 消息应继承当前 group_id
        - 若 last_call_prompt 非空，应将其写入 pending dict 的 "content" 字段，
          并设置 UnifiedMessage 的 text 字段
        - **返回更新后的完整 messages 列表**
        """
        raise NotImplementedError("Subclass must implement handle_after_tool_calls()")

    @abstractmethod
    async def handle_after_loop(
        self,
        final_message: Dict[str, Any],
    ) -> None:
        """对话循环结束后收尾处理
        
        Args:
            final_message: 最终 assistant 消息，格式:
                {
                    "role": "assistant",
                    "content": str,
                    "timestamp": str,
                    "agent_type": str,
                    ...
                }
        
        注意：
        - 应为 final_message 附加 group_id
        - 应批量写盘所有暂存消息
        - 应清空暂存状态（_pending_messages, _current_group_id 等）
        - 通过 self._session_task_handler.log_info() 记录写盘结果
        """
        raise NotImplementedError("Subclass must implement handle_after_loop()")

    async def handle_interruption(self) -> int:
        """中断处理：丢弃本轮所有未写盘的暂存消息，重置内存状态
        
        当对话循环因异常（Exception / CancelledError）中断时，由 Agent 调用。
        与 handle_after_loop 互斥——中断时调用此方法，正常结束时调用 handle_after_loop。
        
        默认实现通过 getattr 清理子类定义的 _pending_messages / _messages /
        _current_group_id 字段。子类可覆盖以执行额外清理。
        
        Returns:
            被丢弃的 pending 消息数量
        """
        pending = getattr(self, "_pending_messages", [])
        discarded = len(pending)

        if pending:
            pending.clear()
        messages = getattr(self, "_messages", [])
        if messages:
            messages.clear()
        if hasattr(self, "_current_group_id"):
            self._current_group_id = None  # type: ignore[attr-defined]

        if self._session_task_handler:
            await self._session_task_handler.log_warning(
                f"[{type(self).__name__}] 中断处理：丢弃 {discarded} 条未写盘消息"
            )

        return discarded

    async def handle_spawn_context_snapshot(self) -> Dict[str, Any]:
        return await self.get_spawn_context_snapshot()

    async def handle_before_llm_call(
        self,
        messages: List["UnifiedMessage"],
        loop_task_handler: "SessionTaskHandlerV2",
    ) -> List["UnifiedMessage"]:
        """每次 LLM 调用前的钩子（可选）

        SoulV6 等 handler 在此执行 in-loop 的过时工具结果驱逐 /
        budget re-balance。默认实现原样返回 messages。

        Args:
            messages: 当前准备发送给 LLM 的消息列表

        Returns:
            可能被修改过（例如压缩 / 替换了 stale tool_results）的消息列表
        """
        return messages

    async def handle_compress(self):
        """上下文压缩处理（预留接口，当前未大量使用）"""
        pass

    # ------------------------------------------------------------------
    # 静态辅助方法：历史记录 → UnifiedMessage 转换
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_content_string(content: str) -> str:
        """检查 content 是否为 Python ast dict/list 格式，若是则转为 JSON 字符串。

        工具输出可能以 Python repr 格式（单引号、True/False/None）存储，
        此方法将其规范化为合法的 JSON 字符串，方便后续 LLM 消费。

        Args:
            content: 原始 content 字符串

        Returns:
            规范化后的字符串（若无法解析则原样返回）
        """
        if not content:
            return content
        stripped = content.strip()
        if not (stripped.startswith(("{", "[")) and stripped.endswith(("}", "]"))):
            return content
        import ast
        import json
        try:
            parsed = ast.literal_eval(stripped)
            if isinstance(parsed, (dict, list)):
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, SyntaxError):
            pass
        return content

    @staticmethod
    def records_to_unified_messages(
        records: list,
    ) -> list:
        """将历史消息 dict 列表转换为 UnifiedMessage 列表。

        支持的消息类型：
        - assistant + tool_calls: 带工具调用的 assistant 消息
        - user + tool_results:   带工具结果的 user 消息
        - user + content_parts:  多模态 user 消息
        - 普通 user / assistant 文本消息

        Args:
            records: 历史消息 dict 列表（每条包含 role, content 等字段）

        Returns:
            List[UnifiedMessage]
        """
        from ..llm import UnifiedMessage, ToolCall, ToolCallResult, ReasoningArtifact
        from ..llm.types import ContentPart, TextPart, ImagePart

        def _deser_part(d: dict) -> ContentPart:
            if d.get("type") == "image":
                return ImagePart(
                    source_type=d.get("source_type", "base64"),
                    data=d.get("data", ""),
                    media_type=d.get("media_type", "image/png"),
                )
            return TextPart(text=d.get("text", ""))

        messages: list = []
        for msg in records:
            role = msg.get("role")
            content = msg.get("content", "") or ""
            tool_calls_raw = msg.get("tool_calls")
            tool_results_raw = msg.get("tool_results")

            if not isinstance(content, str):
                content = str(content)

            if role == "assistant" and tool_calls_raw:
                tcs = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                    for tc in tool_calls_raw
                ]
                messages.append(
                    UnifiedMessage(
                        role="assistant",
                        text=content or None,
                        tool_calls=tcs,
                        reasoning=ReasoningArtifact.from_dict(msg["reasoning"]) if msg.get("reasoning") else None,
                        produced_by_provider=msg.get("produced_by_provider"),
                        produced_by_model=msg.get("produced_by_model"),
                    )
                )

            elif role == "user" and tool_results_raw:
                trs = [
                    ToolCallResult(
                        tool_call_id=tr["tool_call_id"],
                        tool_name=tr["tool_name"],
                        content=BaseContextHandlerV3.normalize_content_string(tr["content"]),
                    )
                    for tr in tool_results_raw
                ]
                messages.append(UnifiedMessage(role="user", tool_results=trs))

            elif role in ("user", "assistant"):
                raw_parts = msg.get("content_parts")
                if raw_parts and role == "user":
                    parts = [_deser_part(p) for p in raw_parts]
                    messages.append(UnifiedMessage(role=role, content_parts=parts))
                elif role == "assistant":
                    # 普通 assistant 文本消息也可能携带思考产物
                    messages.append(UnifiedMessage(
                        role=role,
                        text=content,
                        reasoning=ReasoningArtifact.from_dict(msg["reasoning"]) if msg.get("reasoning") else None,
                        produced_by_provider=msg.get("produced_by_provider"),
                        produced_by_model=msg.get("produced_by_model"),
                    ))
                else:
                    messages.append(UnifiedMessage(role=role, text=content))

        return messages
