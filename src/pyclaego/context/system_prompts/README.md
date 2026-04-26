# `src/context/system_prompts` — System Prompt Templates

Static prompt template strings used by context handlers and agents.

## Files

| File | Constant / function | Used by |
|------|---------------------|---------|
| `default_soul.py` | `DEFAULT_SOUL_SYSTEM_PROMPT` | SoulV5 base system prompt |
| `soulv5_compress.py` | `SOULV5_COMPRESS_PROMPT` | SoulV5 `/compress` command |
| `soulv6.py` | `SOULV6_SYSTEM_PROMPT_SUFFIX` | SoulV6 context handler (appended to V5 base) |
| `simple_v2.py` | `SIMPLE_V2_SYSTEM_PROMPT`, `LAST_CALL_PROMPT` | Main agent system prompt used by SoulV5 + SoulV6; `LAST_CALL_PROMPT` injected by SpawnAgent on the final round |
| `subagent_code_explorer.py` | `CODE_EXPLORER_SYSTEM_PROMPT` | `CodeExplorerSubAgent` |
| `subagent_info_gatherer.py` | `INFO_GATHERER_SYSTEM_PROMPT` | `InfoGathererSubAgent` |

> **Note:** `simple_v2.py` is the shared base system prompt for SoulV5 and SoulV6.
> The name refers to the prompt version, not a removed context handler type.
