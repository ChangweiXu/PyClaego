"""LLM Bash 命令安全审查规则 - 调用 LLM 对 bash 命令进行智能安全分析"""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai.types.chat.chat_completion import ChatCompletion
from anthropic.types import Message

from ..base_rule import BaseSecurityRule, SecurityDecision
from ...logging import get_running_log
from ...llm import UnifiedMessage, ChatResponseV2

_rlog = get_running_log()

# ─────────────────────────────────────────────────────────────────
#  Prompt 常量
# ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是一个专业的 Bash 命令安全审查员。你的任务是分析用户提交的 bash 命令，判断其安全风险等级，并给出详细的解析报告。

请严格按照以下 XML 格式输出，不要输出任何其他内容（不要加 markdown 代码块）：

<review>
  <verdict>safe|warn|deny</verdict>
  <summary>一句话总结命令意图</summary>
  <analysis>
    <command_type>single|pipeline|compound|script</command_type>
    <components>
      <component>
        <cmd><![CDATA[子命令文本]]></cmd>
        <purpose>该子命令的作用</purpose>
        <risk_level>none|low|medium|high</risk_level>
      </component>
    </components>
    <flow><![CDATA[对整体命令的执行流程的文字描述，包括数据流向、条件分支等]]></flow>
    <side_effects>
      <item>副作用描述</item>
    </side_effects>
  </analysis>
  <risk_points>
    <item>风险点描述</item>
  </risk_points>
  <reason><![CDATA[判断为当前 verdict 的详细理由]]></reason>
</review>

XML 输出规则（必须严格遵守）：
1. <cmd>、<flow>、<reason> 这三个字段的内容必须用 CDATA 节包裹：<![CDATA[内容]]>
   - 原因：这些字段可能含有 shell 特殊字符（& | < > ; 等），CDATA 可避免 XML 解析错误
   - 示例：<flow><![CDATA[先执行 mkdir -p /tmp && cp a b，成功后继续]]></flow>
2. 其他字段（verdict、summary、purpose、risk_level、side_effects/item、risk_points/item）
   若内容含有 & < > " ' 等特殊字符，必须用 XML 实体替换：
   & → &amp;   < → &lt;   > → &gt;   " → &quot;   ' → &apos;
3. 不得在 CDATA 节外部出现裸露的 & < > " ' 等特殊字符

verdict 判断标准：
- safe: 命令只读取信息或执行无副作用操作，风险极低
- warn: 命令会修改文件系统或状态，但不构成明显破坏性威胁
- deny: 命令具有高破坏性（删除/格式化/覆盖重要文件、权限提升、网络渗透等）"""

_USER_PROMPT_TMPL = """请审查以下 bash 命令：

<command>
{command}
</command>

执行上下文：
- 工作目录：{cwd}
- 会话 ID：{session_id}"""


# ─────────────────────────────────────────────────────────────────
#  规则实现
# ─────────────────────────────────────────────────────────────────

class LlmBashReviewRule(BaseSecurityRule):
    """LLM Bash 命令安全审查规则

    功能：
    - 调用 LLM 对 bash 命令进行安全分析，输出结构化 XML 结果
    - 根据 LLM 的 verdict（safe / warn / deny）映射到 SecurityDecision
    - LLM 调用失败时按 fallback_action 兜底，不阻断正常流程

    配置示例：
    ```yaml
    rule_type: "llm_bash_review"
    rule_id: "llm_bash_security_review"
    enabled: true
    request_types: ["tool_call"]
    action: "warn"                    # 基础 action（被 LLM verdict 覆盖）

    llm_id: "kimi_code"               # llm.providers 中的 provider ID
    timeout: 30                       # LLM 调用超时（秒）
    fallback_action: "warn"           # LLM 失败时兜底: allow/warn/deny
    deny_on_deny: true                # LLM 返回 deny 时触发真正的 DENY
    include_review_in_reason: true    # 是否在 reason 中包含完整审查内容
    ```
    """

    # bash 类工具名称集合
    _BASH_TOOL_NAMES = frozenset(["bash", "bash_executor", "shell", "sh"])

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)

        self.llm_id: str = rule_config.get("llm_id", "")
        self.timeout: float = float(rule_config.get("timeout", 30))
        self.fallback_action: str = rule_config.get("fallback_action", "warn")
        self.deny_on_deny: bool = rule_config.get("deny_on_deny", True)
        self.include_review_in_reason: bool = rule_config.get("include_review_in_reason", True)

        # 懒加载 LLM 客户端
        self._llm_client = None

        # 缓存最近一次 LLM 审查结果（dict，解析自 XML）
        self._last_review: Optional[Dict[str, Any]] = None
        # 最近一次 matches() 调用产生的决策（与 get_decision() 对应）
        self._last_decision: SecurityDecision = SecurityDecision.ALLOW

        # 获取 workspace_root（与 SecurityHandler 保持一致的计算方式）
        try:
            from ...config import get_config
            _config = get_config()
            _session_cfg = _config.get("session", {})
            log_root = _session_cfg.get("log_root", "~/.pyclaego/.logs")
            self.log_root = Path(log_root).expanduser()
            _ws_root = _session_cfg.get("workspace_root", "~/.pyclaego/workspaces")
            self.workspace_root: Path = Path(_ws_root).expanduser().parent
        except Exception:
            self.log_root = Path("~/.pyclaego/.logs").expanduser()
            self.workspace_root = Path("~/.pyclaego").expanduser()

        if not self.llm_id:
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] 规则 {self.rule_id} 未配置 llm_id，将始终走 fallback_action"
            )

        _rlog.info(
            "core_service",
            f"[LlmBashReviewRule] 初始化规则 {self.rule_id}: "
            f"llm_id={self.llm_id}, timeout={self.timeout}, "
            f"fallback={self.fallback_action}, deny_on_deny={self.deny_on_deny}"
        )

    # ── 公开接口 ────────────────────────────────────────────────

    async def matches(self, request: Dict[str, Any]) -> bool:
        """判断请求是否匹配（需要拦截/警告）

        - 只处理 bash 工具调用
        - 调用 LLM 做安全分析，根据 verdict 决定是否匹配
        - safe → 不匹配（放行）
        - warn → 匹配（警告）
        - deny → 匹配（拒绝，前提是 deny_on_deny=true）
        """
        if not self.is_enabled():
            return False

        # 过滤非 tool_call 请求
        if request.get("type") != "tool_call":
            return False

        # 过滤非 bash 工具
        tool_name = request.get("tool_name", "")
        if tool_name.lower() not in self._BASH_TOOL_NAMES:
            return False

        # 提取命令
        command = self._extract_command(request)
        if not command:
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] 规则 {self.rule_id}: 无法提取命令，跳过审查"
            )
            return False

        # 调用 LLM 审查
        session_id = request.get("session_id", "unknown")
        review = await self._call_llm_review(command, session_id)

        if review is None:
            # LLM 调用失败，走 fallback
            return self._apply_fallback()

        self._last_review = review
        verdict = review.get("verdict", "warn").lower()

        # 根据 verdict 确定决策
        if verdict == "safe":
            self._last_decision = SecurityDecision.ALLOW
            _rlog.info(
                "core_service",
                f"[LlmBashReviewRule] 规则 {self.rule_id}: 命令审查通过 (safe)"
            )
            return False

        elif verdict == "warn":
            self._last_decision = SecurityDecision.WARN
            _rlog.warning(
                "core_service",
                    f"[LlmBashReviewRule] 规则 {self.rule_id}: 命令有风险 (warn) - "
                    f"{review.get('reason', '')[:120]}"
                )
            return True

        elif verdict == "deny":
            if self.deny_on_deny:
                self._last_decision = SecurityDecision.DENY
            else:
                self._last_decision = SecurityDecision.WARN
            _rlog.warning(
                "core_service",
                    f"[LlmBashReviewRule] 规则 {self.rule_id}: 命令被拒绝 (deny) - "
                    f"{review.get('reason', '')[:120]}"
                )
            return True

        else:
            # verdict 值不合法，降级为 warn
            self._last_decision = SecurityDecision.WARN
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] 规则 {self.rule_id}: 未知 verdict={verdict!r}，降级为 warn"
            )
            return True

    def get_decision(self) -> SecurityDecision:
        """返回最近一次 matches() 产生的安全决策"""
        return self._last_decision

    def get_match_reason(self) -> str:
        """返回最近一次审查的原因描述"""
        if self._last_review is None:
            return f"LLM 审查失败，已按 fallback_action={self.fallback_action} 处理"

        reason = self._last_review.get("reason", "")
        verdict = self._last_review.get("verdict", "")
        summary = self._last_review.get("summary", "")

        parts = [f"[LLM审查] verdict={verdict}"]
        if summary:
            parts.append(f"命令摘要：{summary}")
        if reason:
            parts.append(f"理由：{reason}")

        if self.include_review_in_reason:
            risk_points = self._last_review.get("risk_points", [])
            if risk_points:
                parts.append("风险点：" + "；".join(risk_points))

        return " | ".join(parts)

    def get_last_review(self) -> Optional[Dict[str, Any]]:
        """返回最近一次 LLM 审查的完整解析结果"""
        return self._last_review

    # ── 私有方法 ─────────────────────────────────────────────────

    def _get_llm_client(self):
        """懒加载 LLM 客户端"""
        if self._llm_client is not None:
            return self._llm_client

        if not self.llm_id:
            return None

        try:
            from ...config import get_config
            from ...llm import LLMClientFactory

            config = get_config()
            providers = config.get("llm", {}).get("providers", {})

            if self.llm_id not in providers:
                _rlog.error(
                    "core_service",
                    f"[LlmBashReviewRule] llm_id={self.llm_id!r} 不在配置的 providers 中"
                )
                return None

            self._llm_client = LLMClientFactory.create_from_config(providers[self.llm_id])
            _rlog.info(
                "core_service",
                f"[LlmBashReviewRule] 已创建 LLM 客户端: {self.llm_id}"
            )
            return self._llm_client

        except Exception as e:
            _rlog.error(
                "core_service",
                f"[LlmBashReviewRule] 创建 LLM 客户端失败: {e}"
            )
            return None

    def _extract_command(self, request: Dict[str, Any]) -> str:
        """从请求中提取命令字符串（与 BashCommandListRule 逻辑保持一致）"""
        # 优先从 command 字段
        cmd = request.get("command", "")
        if cmd:
            return str(cmd).strip()

        # 从 content 字段
        content = request.get("content", "")
        if content:
            return str(content).strip()

        # 从 tool_args.command
        tool_args = request.get("tool_args", {})
        if isinstance(tool_args, dict):
            cmd = tool_args.get("command", "")
            if cmd:
                return str(cmd).strip()

        # 从 arguments.command
        arguments = request.get("arguments", {})
        if isinstance(arguments, dict):
            cmd = arguments.get("command", "")
            if cmd:
                return str(cmd).strip()

        return ""

    def _build_messages(self, command: str, session_id: str) -> List[Dict[str, str]]:
        """构造发送给 LLM 的 messages 列表（旧版，兼容保留）"""
        user_prompt = _USER_PROMPT_TMPL.format(
            command=command,
            cwd="（未提供）",
            session_id=session_id,
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    def _build_messages_v2(
        self, command: str, session_id: str
    ) -> Tuple[str, List[UnifiedMessage]]:
        """构造 V2 格式的消息（协议无关）

        Returns:
            (system_prompt, [UnifiedMessage(role="user", text=...)])
        """
        user_text = _USER_PROMPT_TMPL.format(
            command=command,
            cwd="（未提供）",
            session_id=session_id,
        )
        return _SYSTEM_PROMPT, [UnifiedMessage(role="user", text=user_text)]

    def _save_llm_call_record_v2(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        start_timestamp: str,
        end_timestamp: str,
        success: bool,
        response: Any = "",
        error: str = "",
        temperature: Optional[float] = None,
    ) -> None:
        """将 LLM 完整调用记录保存为 JSON 文件

        保存路径：workspace_root / "llm_calls" / session_id /
        文件名  ：{YYYYmmdd_HHMMSS_ffffff}-{session_id}-{llm_id}.json

        记录格式与 SecurityHandler._save_llm_call_record_v2 保持一致，
        额外增加 rule_id 字段以标识是哪条安全规则发起的调用。
        """
        try:
            llm_calls_dir = self.log_root / "llm_calls" / session_id
            llm_calls_dir.mkdir(parents=True, exist_ok=True)

            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{timestamp_str}-{session_id}-{self.llm_id}.json"
            filepath = llm_calls_dir / filename

            # 序列化响应对象（与 handler.py 的序列化逻辑对齐）
            if isinstance(response, ChatCompletion):
                response_data: Any = {
                    "id": response.id,
                    "object": response.object,
                    "created": response.created,
                    "model": response.model,
                    "choices": [
                        {
                            "index": choice.index,
                            "message": {
                                "role": choice.message.role,
                                "content": choice.message.content,
                                "tool_calls": [
                                    {
                                        "tool_name": call.function.name,
                                        "arguments": call.function.arguments,
                                    }
                                    for call in choice.message.tool_calls
                                ] if (
                                    hasattr(choice.message, "tool_calls")
                                    and choice.message.tool_calls is not None
                                ) else None,
                            },
                            "finish_reason": choice.finish_reason,
                        }
                        for choice in response.choices
                    ],
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    },
                }
                if (
                    hasattr(response, "prompt_filter_results")
                    and response.prompt_filter_results is not None
                ):
                    response_data["prompt_filter_results"] = response.prompt_filter_results
            elif isinstance(response, Message):
                response_data = {
                    "id": response.id,
                    "type": response.type,
                    "role": response.role,
                    "model": response.model,
                    "content": [
                        {
                            "type": block.type,
                            "text": block.text if hasattr(block, "text") else str(block),
                        }
                        for block in response.content
                    ],
                    "stop_reason": response.stop_reason,
                    "stop_sequence": response.stop_sequence,
                    "usage": {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                }
            else:
                response_data = str(response) if response else ""

            record = {
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "llm_id": self.llm_id,
                "rule_id": self.rule_id,
                "messages": messages,
                "success": success,
                "response": response_data,
                "error": error,
                "temperature": temperature,
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

            _rlog.info(
                "core_service",
                f"[LlmBashReviewRule] LLM V2 调用记录已保存: {filepath}"
            )

        except Exception as e:
            _rlog.error(
                "core_service",
                f"[LlmBashReviewRule] 保存 LLM V2 调用记录失败: {e}"
            )

    async def _call_llm_review(
        self, command: str, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """调用 LLM（V2 接口）并解析 XML 响应，失败返回 None

        使用 chat_completion_v2 统一接口，并将完整调用记录写入
        workspace_root / "llm_calls" / session_id 目录。
        """
        llm_client = self._get_llm_client()
        if llm_client is None:
            _rlog.error(
                "core_service",
                "[LlmBashReviewRule] 无可用的 LLM 客户端，跳过审查"
            )
            return None

        system, messages = self._build_messages_v2(command, session_id)

        # 构造可序列化的消息摘要（用于写入记录）
        serialized_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system}
        ] + [{"role": m.role, "content": m.text} for m in messages]

        start_timestamp = datetime.now().isoformat()

        try:
            v2_resp: ChatResponseV2 = await asyncio.wait_for(
                llm_client.chat_completion_v2(
                    system=system,
                    messages=messages,
                    temperature=0.1,
                ),
                timeout=self.timeout,
            )

            end_timestamp = datetime.now().isoformat()
            self._save_llm_call_record_v2(
                session_id=session_id,
                messages=serialized_messages,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                success=True,
                response=v2_resp.raw_response,
                temperature=0.1,
            )

            raw_text = v2_resp.text or ""
            if not raw_text:
                _rlog.warning(
                    "core_service",
                    "[LlmBashReviewRule] LLM 返回空响应"
                )
                return None

            return self._parse_xml_response(raw_text)

        except asyncio.TimeoutError:
            end_timestamp = datetime.now().isoformat()
            self._save_llm_call_record_v2(
                session_id=session_id,
                messages=serialized_messages,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                success=False,
                error=f"LLM 调用超时 (>{self.timeout}s)",
                temperature=0.1,
            )
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] LLM 调用超时 (>{self.timeout}s)"
            )
            return None

        except Exception as e:
            end_timestamp = datetime.now().isoformat()
            self._save_llm_call_record_v2(
                session_id=session_id,
                messages=serialized_messages,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                success=False,
                error=str(e),
                temperature=0.1,
            )
            _rlog.error(
                "core_service",
                f"[LlmBashReviewRule] LLM 调用异常: {e}"
            )
            return None

    def _extract_response_text(self, response) -> str:
        """从不同类型的 LLM 响应中提取文本"""
        try:
            # OpenAI ChatCompletion
            if hasattr(response, "choices"):
                return response.choices[0].message.content or ""
            # Anthropic Message
            if hasattr(response, "content") and isinstance(response.content, list):
                blocks = response.content
                if blocks:
                    first = blocks[0]
                    return first.text if hasattr(first, "text") else str(first)
            # 字符串兜底
            return str(response) if response else ""
        except Exception:
            return ""

    def _parse_xml_response(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 XML，转为 dict"""
        # 尝试提取 <review>...</review> 片段（防止 LLM 添加了多余文本）
        xml_text = self._extract_xml_block(raw_text)
        if not xml_text:
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] 响应中未找到 <review> 块，原始文本: {raw_text[:200]}"
            )
            return None

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] XML 解析失败: {e}，原始片段: {xml_text[:200]}"
            )
            return None

        result: Dict[str, Any] = {}

        # 顶层字段
        for tag in ("verdict", "summary", "reason"):
            elem = root.find(tag)
            result[tag] = (elem.text or "").strip() if elem is not None else ""

        # 规范化 verdict
        verdict = result.get("verdict", "").lower()
        if verdict not in ("safe", "warn", "deny"):
            _rlog.warning(
                "core_service",
                f"[LlmBashReviewRule] 非法 verdict={verdict!r}，降级为 warn"
            )
            result["verdict"] = "warn"

        # analysis 节点
        analysis_elem = root.find("analysis")
        analysis: Dict[str, Any] = {}
        if analysis_elem is not None:
            ct_elem = analysis_elem.find("command_type")
            analysis["command_type"] = (ct_elem.text or "").strip() if ct_elem is not None else ""

            # components
            components = []
            for comp_elem in analysis_elem.findall("components/component"):
                comp: Dict[str, str] = {}
                for tag in ("cmd", "purpose", "risk_level"):
                    e = comp_elem.find(tag)
                    comp[tag] = (e.text or "").strip() if e is not None else ""
                components.append(comp)
            analysis["components"] = components

            # flow
            flow_elem = analysis_elem.find("flow")
            analysis["flow"] = (flow_elem.text or "").strip() if flow_elem is not None else ""

            # side_effects
            analysis["side_effects"] = [
                (e.text or "").strip()
                for e in analysis_elem.findall("side_effects/item")
                if (e.text or "").strip()
            ]

        result["analysis"] = analysis

        # risk_points
        result["risk_points"] = [
            (e.text or "").strip()
            for e in root.findall("risk_points/item")
            if (e.text or "").strip()
        ]

        return result

    def _extract_xml_block(self, text: str) -> str:
        """从文本中提取第一个完整的 <review>...</review> 片段"""
        match = re.search(r"<review\b[^>]*>.*?</review>", text, re.DOTALL)
        if match:
            return match.group(0)
        return ""

    def _apply_fallback(self) -> bool:
        """LLM 调用失败时，按 fallback_action 设置决策并返回是否匹配"""
        fa = self.fallback_action.lower()
        if fa == "allow":
            self._last_decision = SecurityDecision.ALLOW
            self._last_review = None
            return False
        elif fa == "deny":
            self._last_decision = SecurityDecision.DENY
            self._last_review = None
            return True
        else:  # warn（默认）
            self._last_decision = SecurityDecision.WARN
            self._last_review = None
            return True
