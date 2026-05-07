"""LLM SafePython 命令树安全审查规则。

类似 ``LlmSafeBashReviewRule``，但针对 ``safe_python`` 工具。
继承 ``LlmPythonReviewRule``，复用其 Python 代码审查 prompt 和代码提取逻辑。

与父类的唯一区别：
  - 匹配工具名：``safe_python``（而非 ``python_exec``）
  - LLM prompt 加注：代码已通过 AST 验证 + 安全策略检查，请专注语义风险

QUERY 升级行为继承自 ``LlmBashReviewRule`` → ``LlmPythonReviewRule`` 链，
无需额外覆写。

配置示例：
```yaml
rule_type: "llm_safe_python_review"
rule_id: "llm_safe_python_security_review"
enabled: false
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


from ...llm import UnifiedMessage
from ...logging import get_running_log
from .llm_python_review_rule import LlmPythonReviewRule

_rlog = get_running_log()

# ─────────────────────────────────────────────────────────────────
#  Prompt 常量（在父类 prompt 基础上加注安全上下文）
# ─────────────────────────────────────────────────────────────────

_SAFE_PYTHON_SYSTEM_PROMPT = """你是一个专业的 Python 代码安全审查员。你的任务是分析用户提交的 Python 代码，判断其安全风险等级，并给出详细的解析报告。

注意：此代码已经通过 AST 结构验证和安全策略检查（模块白名单、禁止危险内置函数如 eval/exec/open、禁止 dunder 属性访问、禁止相对导入和通配符导入）。
请专注于代码的语义层面风险：是否存在数据外泄、资源滥用、死循环、逻辑炸弹等静态分析无法检测的问题。

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
2. 其他字段若内容含有 & < > " ' 等特殊字符，必须用 XML 实体替换
3. 不得在 CDATA 节外部出现裸露的 & < > " ' 等特殊字符

verdict 判断标准（注意：结构安全已被 AST 验证保障，仅评估语义风险）：
- safe: 纯计算/数据处理，无数据外泄风险，无资源滥用风险
- warn: 有潜在的性能问题或资源消耗（如大数据量循环），但不构成明显威胁
- deny: 存在数据外泄风险、逻辑炸弹、资源耗尽攻击等恶意行为"""


# ─────────────────────────────────────────────────────────────────
#  规则实现
# ─────────────────────────────────────────────────────────────────

class LlmSafePythonReviewRule(LlmPythonReviewRule):
    """LLM SafePython 代码安全审查规则

    针对 ``safe_python`` 工具的代码审查。
    继承 ``LlmPythonReviewRule`` 的全部 QUERY 升级、代码提取和记录保存逻辑。
    """

    # safe_python 工具名称集合
    _BASH_TOOL_NAMES = frozenset(["safe_python"])  # 复用父类的工具名过滤机制

    # ── 私有方法 ─────────────────────────────────────────────────

    def _build_messages_v2(
        self, command: str, session_id: str
    ) -> tuple[str, list[UnifiedMessage]]:
        """构造 safe_python 专用消息，使用加注安全上下文的 system prompt。

        command 参数实际是 Python 源代码字符串。
        """
        # 复用父类的 user prompt 模板（通过导入）
        from .llm_python_review_rule import _USER_PROMPT_TMPL

        # 判断代码来源
        source = "code_string"

        user_text = _USER_PROMPT_TMPL.format(
            code=command,
            session_id=session_id,
            source=source,
        )
        return _SAFE_PYTHON_SYSTEM_PROMPT, [UnifiedMessage(role="user", text=user_text)]
