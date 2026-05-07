"""Librarian chatbot engine for the notes widget.

Provides a simple tool-calling LLM loop with 4 vault tools:
  vault_search, vault_read, vault_list, vault_create
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .....llm import LLMClientFactory, ToolCall, ToolCallResult, ToolDefinition, UnifiedMessage

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "librarian.system.md").read_text(encoding="utf-8")

_MAX_TOOL_ROUNDS = 5

_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="vault_search",
        description="全文检索笔记库。返回最多20条匹配结果（标题 + 摘要）。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="vault_read",
        description="读取指定路径的笔记，返回纯文本内容。",
        parameters={
            "type": "object",
            "properties": {
                "rel_path": {"type": "string", "description": "笔记的相对路径，例如 ideas/note.bdx"},
            },
            "required": ["rel_path"],
        },
    ),
    ToolDefinition(
        name="vault_list",
        description="获取笔记库所有文件的目录树（扁平路径列表）。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    ToolDefinition(
        name="vault_create",
        description="在笔记库中创建一条新笔记。",
        parameters={
            "type": "object",
            "properties": {
                "rel_path": {"type": "string", "description": "新笔记的相对路径，必须以 .bdx 结尾"},
                "content": {"type": "string", "description": "笔记的纯文本内容"},
            },
            "required": ["rel_path", "content"],
        },
    ),
]


def _flatten_tree(nodes: list[dict], _indent: int = 0) -> list[str]:
    """Recursively flatten file-tree into indented path strings."""
    lines: list[str] = []
    prefix = "  " * _indent
    for node in nodes:
        if node["type"] == "dir":
            lines.append(f"{prefix}{node['name']}/")
            lines.extend(_flatten_tree(node.get("children", []), _indent + 1))
        else:
            lines.append(f"{prefix}{node['rel_path']}")
    return lines


def _make_client():
    """Create an LLM client from the global config. Returns None if not configured."""
    try:
        from .....config import get_config
        llm_cfg: dict = get_config().get("llm") or {}
        providers: dict = llm_cfg.get("providers") or {}
        default_id: str | None = llm_cfg.get("default_provider")
        if not default_id or default_id not in providers:
            # Fall back to first available provider
            if not providers:
                return None
            default_id = next(iter(providers))
        provider_cfg = providers[default_id]
        return LLMClientFactory.create_from_config(provider_cfg)
    except Exception:
        return None


class LibrarianEngine:
    """Stateless per-request engine. Create one per chat turn."""

    def __init__(self, vault: Any):
        self._vault = vault

    async def chat(self, message: str, history: list[dict]) -> str:
        """Run one user turn through the LLM + tool loop.

        Args:
            message: The current user message.
            history: List of {role, content} dicts from previous turns.

        Returns:
            The assistant's reply as a plain string.
        """
        client = _make_client()
        if client is None:
            return "（Librarian 未能连接 LLM：请在配置中设置 llm.providers。）"

        # Build initial message list from history (skip invalid entries)
        messages: list[UnifiedMessage] = []
        for m in history:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            messages.append(UnifiedMessage(
                role=role,
                text=m.get("content", ""),
                content_parts=None,
                tool_calls=None,
                tool_results=None,
            ))
        # Append current turn
        messages.append(UnifiedMessage(
            role="user",
            text=message,
            content_parts=None,
            tool_calls=None,
            tool_results=None,
        ))

        last_text = ""
        for _ in range(_MAX_TOOL_ROUNDS):
            resp = await client.chat_completion_v2(
                system=_SYSTEM_PROMPT,
                messages=messages,
                tool_list=_TOOLS,
            )
            last_text = resp.text or ""

            if not resp.tool_calls:
                return last_text

            # Append assistant message with tool calls
            messages.append(UnifiedMessage(
                role="assistant",
                text=resp.text,
                content_parts=None,
                tool_calls=resp.tool_calls,
                tool_results=None,
            ))

            # Execute all tool calls and collect results
            tool_results: list[ToolCallResult] = []
            for tc in resp.tool_calls:
                result_text = await self._execute(tc)
                tool_results.append(ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=result_text,
                ))

            messages.append(UnifiedMessage(
                role="user",
                text=None,
                content_parts=None,
                tool_calls=None,
                tool_results=tool_results,
            ))

        return last_text or "（已达到最大工具调用轮次）"

    async def _execute(self, tc: ToolCall) -> str:
        """Dispatch a tool call to the vault and return the result as a string."""
        args = tc.arguments
        name = tc.name
        try:
            if name == "vault_search":
                results = await self._vault.search(args.get("query", ""), limit=20)
                if not results:
                    return "没有找到匹配的笔记。"
                lines = [
                    f"- [{r['rel_path']}] {r['title'] or '(无标题)'}: {r['snippet']}"
                    for r in results
                ]
                return "\n".join(lines)

            elif name == "vault_read":
                rel_path = args.get("rel_path", "")
                raw = await self._vault.read(rel_path)
                if raw is None:
                    return f"找不到笔记：{rel_path}"
                # Strip XML tags, return plaintext
                try:
                    from .....note_system.bdx_parser import parse_bdx
                    parsed = parse_bdx(raw)
                    return parsed.plaintext or "(空白笔记)"
                except Exception:
                    return raw[:4000]

            elif name == "vault_list":
                tree = self._vault.file_tree()
                lines = _flatten_tree(tree)
                return "\n".join(lines) if lines else "（笔记库为空）"

            elif name == "vault_create":
                rel_path = args.get("rel_path", "")
                content = args.get("content", "")
                if not rel_path.endswith(".bdx"):
                    rel_path += ".bdx"
                # Build minimal bdx XML
                safe = content.replace("]]>", "]]]]><![CDATA[>")
                xml = "\n".join([
                    '<?xml version="1.0" encoding="utf-8"?>',
                    '<bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">',
                    '  <bdx:body>',
                    '    <bdx:paragraph id="b_0001">',
                    f'      <bdx:content><![CDATA[{safe}]]></bdx:content>',
                    '    </bdx:paragraph>',
                    '  </bdx:body>',
                    '</bdx:doc>',
                ])
                meta = await self._vault.write(rel_path, xml)
                return f"已创建笔记：{rel_path} (doc_id={meta.doc_id})"

            else:
                return f"未知工具：{name}"

        except Exception as e:
            return f"工具执行出错：{e}"
