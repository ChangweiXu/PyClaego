# `src/context/agent_tools` — Agent Tools

Tools that expose agent-spawning capability to the LLM.
These are injected into `SpawnAgent`'s tool list alongside the regular `ToolManager` tools.

## Files

| File | Class | Description |
|------|-------|-------------|
| `base_agent_tool.py` | `AgentBaseTool` | Abstract base for agent tools (extends `BaseTool`) |
| `spawn_subagent_tool.py` | `SpawnSubagentTool` | Creates and drives a subagent; returns `RESULT.md` content |

## `spawn_subagent` — LLM-facing Tool

```
name: spawn_subagent
parameters:
  task_prompt   (str, required)  — full task description for the subagent
  subagent_type (str, required)  — must be a key in SUBAGENT_REGISTRY
  memory_mode   (str, optional)  — "empty" (default) | "inherit"
```

`SpawnSubagentTool.execute()` handles:
1. Generating a unique `subagent_id` (`YYYYMMDD_HHMMSS_xxxxxx`)
2. Creating the subagent workspace directory
3. Instantiating the subagent via `AgentFactory.create_subagent()`
4. Running `subagent.process()` and returning `RESULT.md` as the tool output

`IS_PARALLELIZABLE = True` — multiple subagents can run concurrently;
`SpawnAgent` batches them up to `max_concurrent_subagents`.
