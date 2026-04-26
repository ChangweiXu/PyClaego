# `src/context/memory_tools` — SoulV5 Memory Tools

These tools are injected into the LLM's tool list when `soul_v5` or `soul_v6` context is active.
All tools inherit `SoulV5MemoryBaseTool` and receive a `SoulV5MemoryManager` instance at construction.

## Files

| File | Tool name | Description |
|------|-----------|-------------|
| `soulv5_base.py` | — | `SoulV5MemoryBaseTool` base class |
| `soulv5_query_tool.py` | `soulv5_memory_query` | Search memories by keyword using FTS5 + jieba |
| `soulv5_read_tool.py` | `soulv5_memory_read` | Read a specific memory file by path |
| `soulv5_save_case_tool.py` | `soulv5_memory_save_case` | Save a task case record |
| `soulv5_save_experience_tool.py` | `soulv5_memory_save_experience` | Save a cross-case generalisation |
| `soulv5_update_tool.py` | `soulv5_memory_update` | Update an existing memory file |
| `soulv5_browse_topics_tool.py` | `soulv5_memory_browse_topics` | List the topic directory |
| `soulv5_preferences_tool.py` | `soulv5_memory_update_preferences` | Update the preferences file |
| `soulv5_deprecate_tool.py` | `soulv5_memory_deprecate` | Mark a memory as deprecated |

SoulV6 adds one extra tool, defined separately:

| File | Tool name | Description |
|------|-----------|-------------|
| `soulv6_tool_result_read_tool.py` | `soulv6_tool_result_read` | Read a spilled tool result from disk |

## Tool Instantiation

Tools are instantiated by `SoulV5ContextHandler.handle_before_loop()` and cached in `_memory_tools`.
They are not registered in `ToolManager` — they live only within the context handler.
