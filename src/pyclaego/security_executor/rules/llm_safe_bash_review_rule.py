"""LLM SafeBash command-tree security review rule.

Similar to ``LlmBashReviewRule`` but targets the structured command-tree
input of the ``safe_bash`` tool:

- Matched tool names: ``safe_bash`` / ``safe_bash_executor``.
- When extracting the command, it first tries to invoke the
  ``safe_bash.parser`` to parse the raw ``command_tree`` into an AST and
  submits a normalized textual summary to the LLM, which is more reliable.
- On parser failure it falls back to passing the raw string.

Inherits the LLM invocation / XML parsing / record-saving logic from
``LlmBashReviewRule``; only the matching tool set and command-extraction
logic are overridden.

Example config:
```yaml
rule_type: "llm_safe_bash_review"
rule_id: "llm_safe_bash_security_review"
enabled: false
request_types: ["tool_call"]
action: "warn"
llm_id: "@{llm.default_provider}"
timeout: 30
fallback_action: "warn"
deny_on_deny: true
include_review_in_reason: true
```
"""

from typing import Any, Dict

from .llm_bash_review_rule import LlmBashReviewRule
from ...logging import get_running_log

_rlog = get_running_log()


class LlmSafeBashReviewRule(LlmBashReviewRule):
    """LLM review rule for structured safe_bash command trees."""

    _BASH_TOOL_NAMES = frozenset([
        "safe_bash",
        "safe_bash_executor",
    ])

    def _extract_command(self, request: Dict[str, Any]) -> str:
        """Extract the structured command tree and normalize it into a
        human-readable text for the reviewer LLM.

        Priority:
        1. ``tool_args.command_tree`` / ``arguments.command_tree`` raw text.
        2. ``tool_args.command`` compatibility field (legacy interface).

        When parsing succeeds a structured summary (op/cmd/args/cwd) is
        returned; on failure the raw text is returned unchanged.
        """
        raw = self._get_raw_command_tree(request)
        if not raw:
            return super()._extract_command(request)

        rendered = self._render_tree(raw)
        return rendered or raw

    # ── Internal helpers ──

    def _get_raw_command_tree(self, request: Dict[str, Any]) -> str:
        for key in ("tool_args", "arguments"):
            node = request.get(key)
            if isinstance(node, dict):
                val = node.get("command_tree")
                if val:
                    return str(val).strip()
        return ""

    def _render_tree(self, raw: str) -> str:
        """Try to parse ``raw`` (XML or JSON) via ``safe_bash.parser`` and
        return a normalized summary.

        Returns an empty string on parser failure; the caller decides how to
        fall back.
        """
        try:
            from ...tool.safe_bash.parser import parse
        except Exception as e:  # pragma: no cover - defensive
            _rlog.warning(
                "core_service",
                f"[LlmSafeBashReviewRule] Failed to import safe_bash.parser: {e}"
            )
            return ""

        try:
            tree = parse(raw)
        except Exception as e:
            _rlog.info(
                "core_service",
                f"[LlmSafeBashReviewRule] command_tree parse failed, falling back to raw text: {e}"
            )
            return ""

        lines = ["[safe_bash command tree]"]
        cwd = getattr(tree, "cwd", None)
        if cwd:
            lines.append(f"cwd: {cwd}")

        try:
            self._walk(tree, lines, depth=0)
        except Exception as e:
            _rlog.warning(
                "core_service",
                f"[LlmSafeBashReviewRule] Tree walk failed: {e}"
            )
            return ""

        lines.append("")
        lines.append("[raw command_tree]")
        lines.append(raw)
        return "\n".join(lines)

    def _walk(self, node: Any, lines: list, depth: int) -> None:
        indent = "  " * depth
        op = getattr(node, "op", None) or type(node).__name__
        name = getattr(node, "name", None)
        args = getattr(node, "args", None)
        steps = getattr(node, "steps", None) or getattr(node, "children", None)

        if name is not None:
            arg_str = " ".join(str(a) for a in (args or []))
            lines.append(f"{indent}- cmd: {name} {arg_str}".rstrip())
        else:
            lines.append(f"{indent}- op: {op}")

        if steps:
            for child in steps:
                self._walk(child, lines, depth + 1)
