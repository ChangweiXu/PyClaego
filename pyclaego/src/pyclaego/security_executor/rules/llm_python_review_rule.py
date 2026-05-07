"""LLM Python 代码安全审查规则 - 调用 LLM 对 Python 代码进行智能安全分析

继承自 LlmBashReviewRule，复用其 LLM 调用、XML 解析、QUERY 升级和记录保存基础设施。
与 bash 规则的区别：
  - 匹配工具：python_exec
  - 提取的「命令」是 Python 源代码（code 字符串或 file_path 文件内容）
  - LLM prompt 针对 Python 代码安全分析设计
"""

from typing import Any

from ...llm import UnifiedMessage
from ...logging import get_running_log
from .llm_bash_review_rule import LlmBashReviewRule

_rlog = get_running_log()

# ─────────────────────────────────────────────────────────────────
#  Prompt 常量
# ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是一个专业的 Python 代码安全审查员。你的任务是分析用户提交的 Python 代码，判断其安全风险等级，并给出详细的解析报告。

请严格按照以下 XML 格式输出，不要输出任何其他内容（不要加 markdown 代码块）：

<review>
  <verdict>safe|warn|deny</verdict>
  <summary>一句话总结代码意图</summary>
  <analysis>
    <code_type>script|function|class|module</code_type>
    <imports>
      <item>导入的模块名</item>
    </imports>
    <operations>
      <operation>
        <type>file_io|network|subprocess|system_call|eval_exec|data_processing|other</type>
        <description>操作描述</description>
        <risk_level>none|low|medium|high|critical</risk_level>
      </operation>
    </operations>
    <flow><![CDATA[代码执行流程的文字描述]]></flow>
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
1. <flow>、<reason> 这两个字段的内容必须用 CDATA 节包裹：<![CDATA[内容]]>
   - 原因：这些字段可能含有特殊字符（& | < > ; 等），CDATA 可避免 XML 解析错误
2. 其他字段若内容含有 & < > " ' 等特殊字符，必须用 XML 实体替换：
   & → &amp;   < → &lt;   > → &gt;   " → &quot;   ' → &apos;
3. 不得在 CDATA 节外部出现裸露的 & < > " ' 等特殊字符

verdict 判断标准：
- safe: 纯计算/数据处理，无文件/网络/系统操作，无副作用。例如：print(sum(range(10)))、列表推导、数学运算等
- warn: 有文件读写或网络访问但不具有明显破坏性。例如：读取配置文件、HTTP GET 请求、创建临时文件等
- deny: 具有高破坏性或恶意行为。例如：删除文件、执行系统命令（os.system/subprocess）、网络渗透、数据外泄（发送敏感数据到外部）、资源耗尽（无限循环无退出条件）、绕过安全限制（访问 dunder 属性、修改 __builtins__）"""

_USER_PROMPT_TMPL = """请审查以下 Python 代码：

<code>
{code}
</code>

执行上下文：
- 会话 ID：{session_id}
- 代码来源：{source}"""


# ─────────────────────────────────────────────────────────────────
#  规则实现
# ─────────────────────────────────────────────────────────────────

class LlmPythonReviewRule(LlmBashReviewRule):
    """LLM Python 代码安全审查规则

    功能：
    - 调用 LLM 对 Python 代码进行安全分析，输出结构化 XML 结果
    - 根据 LLM 的 verdict（safe / warn / deny）映射到 SecurityDecision
    - warn / deny 统一升级为 QUERY（询问用户确认），继承自 LlmBashReviewRule
    - LLM 调用失败时按 fallback_action 兜底

    配置示例：
    ```yaml
    rule_type: "llm_python_review"
    rule_id: "llm_python_security_review"
    enabled: true
    request_types: ["tool_call"]
    action: "warn"

    llm_id: "@{llm.default_provider}"
    timeout: 30
    fallback_action: "warn"
    deny_on_deny: false
    include_review_in_reason: true
    query_timeout_s: 60
    ```
    """

    # Python 执行工具名称集合
    _BASH_TOOL_NAMES = frozenset(["python_exec"])  # 复用父类的工具名过滤机制

    # ── 公开接口 ────────────────────────────────────────────────

    # get_decision() / get_query_spec() / get_match_reason() 全部继承自
    # LlmBashReviewRule，warn/deny → QUERY 行为自动生效。

    # ── 私有方法 ─────────────────────────────────────────────────

    def _extract_command(self, request: dict[str, Any]) -> str:
        """从请求中提取 Python 代码

        优先级：
        1. file_path 参数 → 读取文件内容
        2. code 参数 → 直接使用
        3. tool_args / arguments 中的 code 字段
        """
        # 优先处理 file_path
        file_path = self._get_file_path(request)
        if file_path:
            try:
                from pathlib import Path
                p = Path(file_path).expanduser()
                if p.exists() and p.is_file():
                    return p.read_text(encoding="utf-8")
                else:
                    _rlog.warning(
                        "core_service",
                        f"[LlmPythonReviewRule] file_path 不存在或不是文件: {file_path}"
                    )
            except Exception as e:
                _rlog.warning(
                    "core_service",
                    f"[LlmPythonReviewRule] 读取 file_path 失败: {e}"
                )

        # 从 code 字段提取
        code = request.get("code", "")
        if code:
            return str(code).strip()

        # 从 tool_args.code
        tool_args = request.get("tool_args", {})
        if isinstance(tool_args, dict):
            code = tool_args.get("code", "")
            if code:
                return str(code).strip()

        # 从 arguments.code
        arguments = request.get("arguments", {})
        if isinstance(arguments, dict):
            code = arguments.get("code", "")
            if code:
                return str(code).strip()

        return ""

    def _get_file_path(self, request: dict[str, Any]) -> str:
        """提取 file_path 参数"""
        for key in ("tool_args", "arguments"):
            node = request.get(key)
            if isinstance(node, dict):
                val = node.get("file_path")
                if val:
                    return str(val).strip()
        return ""

    def _build_messages_v2(
        self, command: str, session_id: str
    ) -> tuple[str, list[UnifiedMessage]]:
        """构造 Python 代码审查的 V2 格式消息

        command 参数实际是 Python 源代码字符串。

        Returns:
            (system_prompt, [UnifiedMessage(role="user", text=...)])
        """
        user_text = _USER_PROMPT_TMPL.format(
            code=command,
            session_id=session_id,
            source="code_string",
        )
        return _SYSTEM_PROMPT, [UnifiedMessage(role="user", text=user_text)]

    def _save_llm_call_record_v2(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        start_timestamp: str,
        end_timestamp: str,
        success: bool,
        response: Any = "",
        error: str = "",
        temperature: float | None = None,
    ) -> None:
        """将 LLM 完整调用记录保存为 JSON 文件

        保存路径：log_root / "python_review_calls" / session_id /
        区别于 bash 规则的 "bash_review_calls" 目录。
        """
        import json
        from datetime import datetime

        from ...llm import serialize_llm_response

        try:
            llm_calls_dir = self.log_root / "python_review_calls" / session_id
            llm_calls_dir.mkdir(parents=True, exist_ok=True)

            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{timestamp_str}-{session_id}-{self.llm_id}.json"
            filepath = llm_calls_dir / filename

            response_data: Any = serialize_llm_response(response)

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
                f"[LlmPythonReviewRule] LLM V2 调用记录已保存: {filepath}"
            )

        except Exception as e:
            _rlog.error(
                "core_service",
                f"[LlmPythonReviewRule] 保存 LLM V2 调用记录失败: {e}"
            )
