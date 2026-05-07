"""Tool Call Parser - 工具调用解析器

提供统一的工具调用文本解析标准，供 Agent 等模块复用。

推荐格式（XML 参数，适合复杂字符串）:
<tool name="bash">
  <arg name="command"><![CDATA[cat "a\"b.txt" && echo done]]></arg>
  <arg name="timeout" type="int">30</arg>
</tool>

兼容格式（旧 JSON 参数体）:
<tool name="bash">
{
  "command": "ls -la"
}
</tool>
"""

import re
from typing import Any

from ..logging import get_running_log

_rlog = get_running_log()


TOOL_CALL_PROMPT = """# 可用工具
当遇到需要工具协助的任务时，使用以下格式调用工具：

<tool name="工具名称">
  <arg name="参数名">参数值</arg>
</tool>

支持的工具如下：
"""


class ToolCallParser:
    """工具调用解析器"""

    # 匹配 <tool name="...">...</tool> 格式（正文支持任意内容）
    TOOL_PATTERN = re.compile(
        r'<tool\s+name="([^"]+)">\s*(.*?)\s*</tool>',
        re.DOTALL | re.MULTILINE,
    )

    # 匹配 XML 参数: <arg name="...">...</arg>（支持可选 type）
    ARG_PATTERN = re.compile(
        r'<arg\s+name="([^"]+)"(?:\s+type="([^"]+)")?>\s*(.*?)\s*</arg>',
        re.DOTALL | re.MULTILINE,
    )

    @classmethod
    def _parse_xml_arg_value(cls, raw_value: str, value_type: str | None) -> Any:
        """解析 XML arg 值，支持 type 与 CDATA。"""
        value = raw_value.strip()

        # 解包 CDATA
        cdata_match = re.match(r'^<!\[CDATA\[(.*)\]\]>$', value, re.DOTALL)
        if cdata_match:
            value = cdata_match.group(1)

        t = (value_type or "").strip().lower()
        if not t or t in ("str", "string"):
            return value

        if t == "int":
            return int(value)
        if t == "float":
            return float(value)
        if t in ("bool", "boolean"):
            return value.lower() in ("true", "1", "yes", "on")
        if t == "json":
            import json

            return json.loads(value)

        # 未知类型回退为字符串
        return value

    @classmethod
    def parse_tool_calls(cls, text: str) -> list[dict[str, Any]]:
        """解析文本中的所有工具调用

        Args:
            text: LLM 响应文本

        Returns:
            工具调用列表，每项包含: {"tool_name": str, "tool_args": dict}
        """
        tool_calls: list[dict[str, Any]] = []

        for match in cls.TOOL_PATTERN.finditer(text):
            tool_name = match.group(1)
            tool_body = match.group(2).strip()

            # 1) 优先解析 XML 参数格式
            arg_matches = list(cls.ARG_PATTERN.finditer(tool_body))
            if arg_matches:
                tool_args: dict[str, Any] = {}
                for arg_match in arg_matches:
                    arg_name = arg_match.group(1)
                    arg_type = arg_match.group(2)
                    arg_raw_value = arg_match.group(3)
                    try:
                        arg_value = cls._parse_xml_arg_value(arg_raw_value, arg_type)
                    except Exception as e:
                        _rlog.error("core_service", f"[ToolCallParser] XML 参数类型转换失败: {tool_name}.{arg_name}, 错误: {e}")
                        arg_value = arg_raw_value.strip()
                    tool_args[arg_name] = arg_value

                tool_calls.append({"tool_name": tool_name, "tool_args": tool_args})
                continue

            # 2) 回退兼容旧 JSON 参数格式
            try:
                import json

                tool_args = json.loads(tool_body)
                tool_calls.append({"tool_name": tool_name, "tool_args": tool_args})
            except json.JSONDecodeError as e:
                _rlog.error("core_service", f"[ToolCallParser] 解析工具参数失败: {tool_name}, 错误: {e}")
                continue

        return tool_calls

    @classmethod
    def has_tool_calls(cls, text: str) -> bool:
        """检查文本中是否包含工具调用"""
        return bool(cls.TOOL_PATTERN.search(text))

    @classmethod
    def remove_tool_tags(cls, text: str) -> str:
        """移除文本中的所有工具调用标签"""
        return cls.TOOL_PATTERN.sub("", text).strip()
