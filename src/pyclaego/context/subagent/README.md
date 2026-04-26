# `src/context/subagent` — Subagent Context Handlers

Context handlers for short-lived subagents created by `SpawnAgent`.
These implement the `BaseContextHandlerV3` lifecycle but differ from main-agent contexts:

- **No history restore** — each subagent starts fresh (or from an inherited snapshot).
- **No memory tools** — the tool set comes from `ToolManager` only.
- **Isolated workspace** — each subagent gets its own directory under `session_workspace/subagents/<subagent_id>/`.

## Files

| File | Class | Used by |
|------|-------|---------|
| `base_subagent_context.py` | `BaseSubAgentContextHandler` | All subagents |
| `info_gatherer_context.py` | `InfoGathererContextHandler` | `InfoGathererSubAgent` |
| `code_explorer_context.py` | `CodeExplorerContextHandler` | `CodeExplorerSubAgent` (extends `InfoGathererContextHandler`) |

## Memory Modes

Controlled by `memory_mode` in the `spawn_subagent` tool call:

| Mode | Behaviour |
|------|-----------|
| `empty` (default) | Subagent system prompt only; empty message history |
| `inherit` | Parent context snapshot injected as initial messages |

## Tool Restrictions

`InfoGathererContextHandler` limits the tool set to read-only tools:
`read_file`, `web_fetch`, `web_search`, `list_directory`, `glob`, `search_text`, `find_line`.

`CodeExplorerContextHandler` additionally allows workspace writes (`write_file`, `mkdir`).
