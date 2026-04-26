# `src/agent` — Agent Module

Provides the agent layer that drives LLM conversation loops, tool execution, and subagent orchestration.

## Architecture

```
agent/
├── base_agent.py          # BaseAgent — abstract interface
├── simple_agent.py        # SimpleAgent — tool-calling loop (internal base)
├── spawn_agent.py         # SpawnAgent — primary agent (inherits SimpleAgent)
├── agent_factory.py       # AgentFactory — creates agents from config
└── subagent/
    ├── base_subagent.py           # BaseSubAgent — short-lived subagent base
    ├── info_gatherer_subagent.py  # InfoGathererSubAgent — web search / read
    └── code_explorer_subagent.py  # CodeExplorerSubAgent — read-only code analysis
```

## Agents

### `SpawnAgent` — primary agent

The production agent. Inherits `SimpleAgent`'s tool-calling loop and adds concurrent subagent dispatch.

**Tool-calling loop steps:**

| Step | Description |
|------|-------------|
| A | Context handler intercepts memory tool calls (via `handle_after_llm_call`) |
| B | Remaining calls split into *normal* tools and *agent* tools (`AGENT_TOOL_NAMES`) |
| C | Normal tools execute sequentially |
| D | Agent tools (spawn requests) execute concurrently in batches of `max_concurrent_subagents` |
| E | All results merged and returned to LLM |

**Configuration:**
```yaml
agent:
  type: spawn
  spawn:
    max_concurrent_subagents: 3   # >0 required to activate SpawnAgent
    max_tool_rounds: 20
  llm: my_llm_id
```

### `SimpleAgent` — internal base class

Implements the core tool-calling loop (`process()` → LLM call → tool execution → repeat).
Not registered in `AgentFactory` and not user-facing. `SpawnAgent` inherits from it.

---

## Subagents

Subagents are short-lived workers created by `SpawnAgent` via the `spawn_subagent` tool.
Each subagent runs in an isolated workspace (`session_workspace/subagents/<subagent_id>/`)
and writes a `RESULT.md` on completion (or failure).

### Registered types (`SUBAGENT_REGISTRY`)

| Type | Class | Purpose |
|------|-------|---------|
| `info_gatherer` | `InfoGathererSubAgent` | Web search, page fetching, information synthesis |
| `code_explorer` | `CodeExplorerSubAgent` | Read-only code analysis, workspace writes allowed |

### Adding a new subagent type

1. Create `src/agent/subagent/my_subagent.py` extending `BaseSubAgent`.
2. Create a matching context handler in `src/context/subagent/`.
3. Register in `src/agent/subagent/__init__.py`:
   ```python
   SUBAGENT_REGISTRY["my_type"] = MySubAgent
   ```

---

## `AgentFactory`

Creates agent instances from config. Only `spawn` is registered as a user-facing type.

```python
from pyclaego.agent import AgentFactory

agent = AgentFactory.create_agent(agent_config, session_id)
subagent = AgentFactory.create_subagent(
    subagent_type, session_id, subagent_id, workspace_path, base_config
)
```
