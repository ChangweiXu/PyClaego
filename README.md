# PyClaego

**A session-based AI agent framework with persistent memory, concurrent subagents, and a pluggable context system.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## Overview

PyClaego is a Python framework for building AI agents that:
- Run tool-calling loops against any LLM (OpenAI, Anthropic, Gemini, DeepSeek, Kimi)
- Maintain long-term memory across sessions via an MD-file + SQLite index (SoulV5/V6)
- Spawn concurrent subagents for parallelisable sub-tasks
- Expose sessions over WebSocket, REST, TUI, and Feishu (Lark) IM

---

## Architecture

```
┌────────────────────────────────────────────────┐
│               Entry Points                      │
│  core_server.py  web_server.py  tui_client.py  │
│  feishu_gateway.py                              │
└────────────────┬───────────────────────────────┘
                 │ WebSocket / HTTP
┌────────────────▼───────────────────────────────┐
│  CoreScheduler — session routing & broadcast   │
│  SessionManager — lifecycle, persistence       │
└────────────────┬───────────────────────────────┘
                 │
┌────────────────▼───────────────────────────────┐
│  Session — message queue, slash commands, cron │
│  ├─ SpawnAgent — tool loop + subagent dispatch │
│  │   └─ SimpleAgent (internal base)            │
│  ├─ ContextHandler (SoulV5 / SoulV6)           │
│  │   ├─ MemoryManager + MemoryRecaller         │
│  │   ├─ BudgetAllocator (V6)                   │
│  │   └─ ToolResultStore / StaleEvictor (V6)    │
│  └─ SecurityHandler                            │
│      ├─ PathResolver ({{WORKSPACE}}, …)        │
│      └─ Rules: bash / workspace / network /    │
│               secret / subagent depth / …      │
└────────────────┬───────────────────────────────┘
                 │
┌────────────────▼───────────────────────────────┐
│  ToolManager — 20+ registered tools            │
│  LLMClientFactory — 6 LLM providers            │
│  TaskManager — task tree + streaming           │
└────────────────────────────────────────────────┘
```

### Layer overview

| Layer | Modules |
|-------|---------|
| Infrastructure | `config`, `logging` |
| Capabilities | `llm`, `tool`, `skill`, `task_manager`, `message` |
| Security & context | `security_executor`, `context` |
| Agent & session | `agent`, `session` |
| Entry points | `core`, `web` |

---

## Quick Start

### 1. Install

**From source (recommended for development):**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tui]"          # editable install with TUI client
```

After installing, three commands are available from **any directory**:
```
pyclaego-server   — core WebSocket scheduler
pyclaego-tui      — TUI client (requires [tui] extra)
pyclaego-feishu   — Feishu IM gateway
```

**Runtime only (no TUI):**
```bash
pip install -e .
# or
pip install -r requirements.txt
```

### 2. Bootstrap

```bash
cd pyclaego/
python bootstrap.py
```

This creates `~/.pyclaego/` with config files and data directories, then prints
the next steps. The script is idempotent — safe to run multiple times.

After bootstrapping, edit your API keys in `~/.pyclaego/.config.d/llm.yaml`.

Minimal `~/.pyclaego/config.yaml` snippet:

```yaml
llm:
  providers:
    my_llm:
      api: anthropic              # openai | anthropic | deepseek | gemini | kimi_anthropic
      api_key: ${ANTHROPIC_API_KEY:}
      model: claude-sonnet-4-5
      max_context_tokens: 200000

agent:
  type: spawn
  llm: my_llm
  spawn:
    max_concurrent_subagents: 3
    max_tool_rounds: 20

context:
  type: soul_v6                   # soul_v5 | soul_v6
  soul_v6:
    keep_groups: 10
```

### 3. Run

```bash
# Terminal 1 — core server
pyclaego-server

# Terminal 2 — TUI client
pyclaego-tui
```

Alternatively, from the project root:
```bash
cd pyclaego
python -m core_server   # or: python core_server.py
python -m tui_client    # or: python tui_client.py
```

### 4. Slash commands

| Command | Description |
|---------|-------------|
| `/stop` | Cancel current task, clear queue |
| `/help` | Show all commands |
| `/compress` | Manually compress conversation history |
| `/llm <id>` | Switch LLM provider at runtime |
| `/cron` | Manage cron jobs |
| `/pin` / `/unpin` | Pin/unpin a message (SoulV6) |
| `/close_loop` | Close an open loop (SoulV6) |
| `/memories` | List recent memories |
| `/forget` | Delete a memory |
| `/export_memory` | Export memory to file |

---

## Project Structure

```
pyclaego/
├── config.example.yaml          # annotated config template
├── core_server.py               # WebSocket core server entry point
├── web_server.py                # FastAPI web server entry point
├── tui_client.py                # Textual TUI client entry point
├── feishu_gateway.py            # Feishu (Lark) IM gateway
├── skills/                      # global skill library
│
└── src/
    ├── agent/                   # SpawnAgent, SimpleAgent (base), subagents
    │   └── subagent/            # InfoGathererSubAgent, CodeExplorerSubAgent
    ├── context/                 # SoulV5 / SoulV6 context handlers + memory
    │   ├── memory_tools/        # soulv5_* + soulv6_tool_result_read tools
    │   ├── agent_tools/         # spawn_subagent tool
    │   ├── system_prompts/      # system prompt templates
    │   └── subagent/            # subagent context handlers
    ├── tool/                    # 20+ tools (file, bash, web, pdf, image …)
    │   └── safe_bash/           # structured bash executor with security checks
    ├── llm/                     # unified LLM client (6 providers)
    ├── session/                 # session lifecycle, slash commands, cron
    ├── core/                    # CoreScheduler (WebSocket hub)
    ├── web/                     # FastAPI REST + WebSocket endpoints
    ├── message/                 # TUI client, Feishu client/gateway
    ├── security_executor/       # SecurityHandler + 10 security rules
    ├── skill/                   # Skill loading and management
    ├── task_manager/            # task tree, streaming, artifact store
    ├── config/                  # ConfigManager (YAML + env + !include)
    ├── logging/                 # LogManager + RunningLog
    └── utility/                 # session ID validation helpers
```

---

## Context Strategies

Two production context strategies are provided:

| Strategy | Config key | Description |
|----------|------------|-------------|
| **SoulV5** | `soul_v5` | Multi-layer MD file tree memory (preferences, topics, cases, experiences) + SQLite FTS5 recall |
| **SoulV6** | `soul_v6` | SoulV5 + tool result disk spillover, stale eviction, entity cards, open loops, turn briefs, write-conflict review |

See [src/context/README.md](src/context/README.md) for full details.

---

## Agent System

`SpawnAgent` is the primary agent. It extends `SimpleAgent`'s tool-calling loop with:
- Concurrent subagent dispatch via `spawn_subagent` tool
- Configurable concurrency (`max_concurrent_subagents`)
- Built-in subagent types: `info_gatherer`, `code_explorer`

See [src/agent/README.md](src/agent/README.md) for full details.

---

## Tools

20+ built-in tools registered in `ToolManager`:

| Category | Tools |
|----------|-------|
| File system | `read_file`, `write_file`, `file_edit`, `file_delete`, `copy_move`, `file_info`, `mkdir`, `list_directory`, `glob`, `find_line`, `search_text` |
| Execution | `safe_bash`, `bash`, `python_exec` |
| Web | `web_fetch` (cached), `web_search`, `download_file` |
| Media | `read_image_base64`, `read_pdf` |

---

## LLM Providers

| Config `api` | Provider |
|---|---|
| `openai` | OpenAI and OpenAI-compatible endpoints |
| `anthropic` | Anthropic Claude |
| `gemini` | Google Gemini |
| `deepseek` | DeepSeek (reasoning_content dialect) |
| `deepseek_anthropic` | DeepSeek via Anthropic-compatible endpoint |
| `kimi_anthropic` | Kimi Code via Anthropic-compatible endpoint |

---

## License

MIT — see [LICENSE](LICENSE).

