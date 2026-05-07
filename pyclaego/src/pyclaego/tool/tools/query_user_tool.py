"""query_user tool — agent-initiated user confirmation.

The agent calls this tool to present the user with a question and a list of
choices.  Execution suspends until the user replies (via the same channel that
carries normal chat messages), then returns the chosen value as the tool result.

The tool *must* be executed through ``SecurityHandler.request_tool_call``
(or _v2), **not** through the raw ``ToolManager``, because the suspension
mechanism lives in ``SecurityHandler``.  To avoid the security rule pipeline
triggering a nested query, ``query_user`` bypasses the QUERY decision: it
directly enqueues a ``PendingQuery`` and awaits its future.

``session_id`` is injected by ``SecurityHandler`` via the ``_session_id``
keyword argument (not exposed in the LLM tool schema).
"""

from __future__ import annotations

import uuid
from typing import Any

from ...logging import get_running_log
from ..base_tool import BaseTool, ToolResult, ToolStatus

_rlog = get_running_log()

# Schema visible to the LLM
QUERY_USER_DESCRIPTION = """Ask the user a question and wait for their reply.

Use this tool when you need explicit user confirmation or a choice before
proceeding.  The user will see the prompt and a set of option buttons (on
supported clients) or plain text options.

Returns the ``value`` of the option the user selected.

**IMPORTANT – one question per call**: Each call must contain exactly one
question in ``prompt``.  If you have multiple independent questions, make
separate sequential ``query_user`` calls.  Do NOT embed multiple questions
or multi-part prompts in a single call.
"""

QUERY_USER_PARAMETERS = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "The question or statement shown to the user.",
        },
        "choices": {
            "type": "array",
            "description": "List of choices the user can pick from.",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "Machine-readable value returned when chosen."},
                    "label": {"type": "string", "description": "Human-readable label shown to the user."},
                    "description": {"type": "string", "description": "Optional extra explanation."},
                },
                "required": ["value", "label"],
            },
        },
        "default": {
            "type": "string",
            "description": "Value returned automatically on timeout (optional).",
        },
        "timeout_s": {
            "type": "integer",
            "description": "Seconds to wait before auto-resolving with default (0 = wait forever).",
        },
    },
    "required": ["prompt", "choices"],
}


class QueryUserTool(BaseTool):
    """Agent-facing tool that suspends execution to ask the user a question.

    This tool is **special**: it does not perform any I/O by itself.  Its
    ``execute`` method enqueues a :class:`PendingQuery` in ``QueryService`` and
    awaits the future.  ``SecurityHandler.request_tool_call`` passes the current
    ``session_id`` as the ``_session_id`` keyword argument so the query can be
    routed to the correct widget queue.
    """

    IS_READONLY: bool = True   # no side effects on the file system
    IS_PARALLELIZABLE: bool = False

    def __init__(self, tool_config: dict[str, Any]) -> None:
        super().__init__(tool_config)

    # ------------------------------------------------------------------
    # BaseTool interface
    # ------------------------------------------------------------------

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.config.get("tool_name", "query_user"),
            "description": QUERY_USER_DESCRIPTION,
            "parameters": QUERY_USER_PARAMETERS,
        }

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        return super().mask_output(raw_output, path_mask_map)

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Suspend until user replies.

        ``_session_id`` must be passed by the caller (SecurityHandler); it is
        NOT part of the LLM-visible schema.
        """
        from ...security_executor.query_service import (
            CANCEL_SENTINEL,
            Choice,
            PendingQuery,
            get_query_service,
        )

        session_id: str | None = kwargs.pop("_session_id", None)
        if not session_id:
            return ToolResult(
                status=ToolStatus.ERROR,
                error="query_user: _session_id not provided (internal error)",
            )

        # Optional caller identity (e.g. "SubAgent xyz") injected by SecurityHandler.
        # Used to prefix the prompt so the user can disambiguate when multiple
        # subagents enqueue queries concurrently.
        subagent_label: str | None = kwargs.pop("_subagent_label", None)

        prompt: str = kwargs.get("prompt", "")
        raw_choices: list[dict[str, Any]] = kwargs.get("choices", [])
        default: str | None = kwargs.get("default")
        timeout_s: int | None = kwargs.get("timeout_s")

        if not prompt or not raw_choices:
            return ToolResult(
                status=ToolStatus.ERROR,
                error="query_user: 'prompt' and 'choices' are required",
            )

        # Prefix prompt with caller identity (only when running as a subagent).
        if subagent_label:
            prompt = f"[来自 {subagent_label}] {prompt}"

        choices = [
            Choice(
                value=c["value"],
                label=c.get("label", c["value"]),
                description=c.get("description", ""),
            )
            for c in raw_choices
        ]

        pending = PendingQuery(
            query_id=str(uuid.uuid4()),
            session_id=session_id,
            origin="tool",
            tool_name="query_user",
            tool_args=None,
            prompt=prompt,
            choices=choices,
            deny_values=[],          # no values are inherently "deny" for agent-initiated queries
            default=default,
            timeout_s=timeout_s,
        )

        qs = get_query_service()
        query_id = await qs.enqueue(pending)
        _rlog.info(
            "core_service",
            f"[QueryUserTool] waiting for user reply query_id={query_id} session={session_id}",
        )

        chosen = await qs.wait_resolved(query_id)
        _rlog.info(
            "core_service",
            f"[QueryUserTool] resolved query_id={query_id} chosen={chosen!r}",
        )

        if chosen == CANCEL_SENTINEL:
            return ToolResult(
                status=ToolStatus.ERROR,
                error="query_user: cancelled by user (/stop)",
            )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=chosen,
        )
