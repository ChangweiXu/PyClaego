# `src/context` — Context Module

Manages conversation history, memory, and tool-call context for each session.
Two production context strategies are provided: **SoulV5** and **SoulV6**.

## Directory Structure

```
context/
├── base_context.py              # BaseContextHandlerV3 — lifecycle interface
├── context_factory.py           # ContextFactory — creates handlers from config
├── history_manager.py           # HistoryFileManager — history.json / .jsonl I/O
├── token_counter.py             # TokenCounter — tiktoken-based token budget
│
├── soulv5_context_handler.py    # SoulV5ContextHandler
├── soulv5_memory_manager.py     # SoulV5MemoryManager (singleton)
├── soulv5_memory_recaller.py    # SoulV5MemoryRecaller — keyword + FTS + LLM rerank
│
├── soulv6_context_handler.py    # SoulV6ContextHandler (extends V5)
├── soulv6_memory_manager.py     # SoulV6MemoryManager (singleton, independent)
├── soulv6_memory_recaller.py    # SoulV6MemoryRecaller — 10-stage recall pipeline
├── soulv6_budget_allocator.py   # SoulV6BudgetAllocator — per-tenant token budget
├── soulv6_tool_result_store.py  # SoulV6ToolResultStore — disk spillover for large results
├── soulv6_stale_evictor.py      # SoulV6StaleEvictor — KEEP / SUMMARIZE / DROP decisions
├── soulv6_turn_brief.py         # SoulV6TurnBriefSynthesizer — async turn summary
├── soulv6_open_loops.py         # SoulV6OpenLoopsStore — unresolved question tracking
├── soulv6_entity_cards.py       # SoulV6EntityCardStore — entity card persistence
├── soulv6_write_review.py       # SoulV6MemoryWriteReview — pre-write conflict check
├── soulv6_metrics.py            # SoulV6MetricsCollector — observability primitives
│
├── memory_tools/                # SoulV5 memory tool implementations (see memory_tools/README.md)
├── agent_tools/                 # SpawnSubagentTool and base (see agent_tools/README.md)
├── system_prompts/              # System prompt templates (see system_prompts/README.md)
└── subagent/                    # Subagent context handlers (see subagent/README.md)
```

---

## Registered Context Types

| Config key | Handler class | Use case |
|------------|---------------|----------|
| `soul_v5` | `SoulV5ContextHandler` | Multi-layer memory; MD file tree |
| `soul_v6` | `SoulV6ContextHandler` | V5 + tool result spillover, eviction, entity cards, open loops |

---

## SoulV5

Multi-layer memory backed by a Markdown file tree on disk.

**Memory layers:**

| Layer | Description |
|-------|-------------|
| `preferences` | User habits, style preferences |
| `topics` | Subject-level summaries |
| `cases` | Specific task records |
| `experiences` | Cross-case generalizations |

**Recall pipeline:** jieba keyword extraction → SQLite FTS5 search → optional LLM reranking.

**Configuration:**
```yaml
context:
  type: soul_v5
soul_v5:
  keep_groups: 10
  memory_root: ~/.pyclaego/memory_v5
  auto_compress_threshold: 40
```

**Memory tools exposed to LLM:**

| Tool | Action |
|------|--------|
| `soulv5_memory_query` | Search memories by keyword |
| `soulv5_memory_read` | Read a specific memory file |
| `soulv5_memory_save_case` | Save a task case |
| `soulv5_memory_save_experience` | Save a generalised experience |
| `soulv5_memory_update` | Update an existing memory |
| `soulv5_memory_browse_topics` | List topic directory |
| `soulv5_memory_update_preferences` | Update preference file |
| `soulv5_memory_deprecate` | Mark a memory as deprecated |

---

## SoulV6

Extends SoulV5 with a full observability and long-context management stack.
V5 is not modified; V6 uses inheritance and method overrides.

**Additional capabilities over V5:**

| Feature | Component |
|---------|-----------|
| Disk spillover for large tool results | `SoulV6ToolResultStore` + `soulv6_tool_result_read` tool |
| Stale result eviction (KEEP / SUMMARIZE / DROP) | `SoulV6StaleEvictor` |
| Per-tenant token budget allocation | `SoulV6BudgetAllocator` |
| Async turn brief synthesis | `SoulV6TurnBriefSynthesizer` |
| Open loop tracking (unresolved questions) | `SoulV6OpenLoopsStore` |
| Entity card store (person / project / concept) | `SoulV6EntityCardStore` |
| Pre-write conflict review | `SoulV6MemoryWriteReview` |
| Turn-level observability metrics | `SoulV6MetricsCollector` |
| 10-stage hierarchical recall pipeline | `SoulV6MemoryRecaller` |

**Configuration:**
```yaml
context:
  type: soul_v6
soul_v6:
  keep_groups: 10
  memory_root: ~/.pyclaego/memory_v6
  auto_compress_threshold: 40
  budget:
    system: 2000
    preferences: 500
    recall: 3000
    entity_cards: 800
```

---

## Lifecycle Hooks (`BaseContextHandlerV3`)

Context handlers are driven by the agent via these hooks:

```
handle_before_loop       → build system prompt, load history, init tools
handle_after_llm_call    → intercept memory tool calls; stash assistant message
handle_memory_tool_calls → execute memory tools; return results
handle_after_tool_calls  → stash tool result messages
handle_after_loop        → persist group to disk; trigger auto-compress
handle_compress          → manual /compress entry point
```

---

## Adding a New Context Type

1. Create `src/context/my_context.py` extending `BaseContextHandlerV3`.
2. Register in `src/context/__init__.py`:
   ```python
   from .my_context import MyContextHandler
   ContextFactory.register_handler("my_type", MyContextHandler)
   ```
3. Use in session config: `context: { type: my_type }`.
