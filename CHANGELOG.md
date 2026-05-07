# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-05-07

Major release: PyClaego evolves from a single-loop agent framework into a centralized, observable Agent control plane with parallel task execution, a modern web dashboard, and a pluggable skill/tool-agent ecosystem.

### Added
- **ToolAgent runtime**: independent, parallel-executable capability units that supersede in-loop SKILL execution; eliminates serial bottlenecks for complex multi-step tasks.
  - Built-in tool agents: `code_explorer`, `echo`, `pipeline`.
  - `tool_agent_distiller` skill for distilling workflows into new ToolAgents.
- **Skill system** (`skills/builtin/`): packaged domain knowledge invoked on demand, including `arxiv-html-to-md-summary`, `code-reading-guide`, `hf-daily-papers`, `skill-creator`, `widget-cron-setup`.
- **LLM Router** (`llm_router/`): unified routing across providers with config-driven model selection, request masking, and usage accounting.
- **Note system** (`note_system/`) with TipTap/BlockNote-compatible document model and converter.
- **Personal Space** (`personal_space/`): per-user widget runtime with cron scheduling, widget store, and gateway APIs.
- **Task Manager** (`task_manager/`): first-class task lifecycle, ownership/belonging, and concurrent execution.
- **Security Executor** upgrades: concurrent query service and hardened safe-bash edge cases.
- **Web Dashboard** (`pyclaego/dashboard/`): new React + Vite + TypeScript frontend with TanStack Query, BlockNote editor, JSON-Schema forms, and live WebSocket message streams; replaces the previous static HTML pages.
- **Web backend** (`web/`): FastAPI app with task API, task bridge, task subscriber, and WebSocket endpoints for full execution observability.
- **CLI entry points**: `pyclaego-core`, `start_core.sh`, `start_dashboard.sh`, and a one-shot `scripts/install.sh` bootstrap that provisions `~/.pyclaego` and copies built-in resources.
- **Context layer** (`context/`): summarizing artifact store and token counters (message-aware and raw).
- **Config v2** (`config/`): layered `.config.d/` overlays with hot-reload-friendly manager.
- **Tooling**: extensive built-in tools incl. `web_fetch_tool_v2/v3`, `web_search_tool`, `write_file_tool`, and an expanded test suite covering routing, widgets, tasks, notes, and security.

### Changed
- Reorganized package layout under `src/pyclaego/` with dedicated subpackages for `agent`, `cli`, `command`, `config`, `context`, `core`, `llm`, `llm_router`, `logging`, `message`, `note_system`, `personal_space`, `security_executor`, `skill`, `task_manager`, `tool`, `tool_agent`, `utility`, `web`.
- Execution model is now message-stream first: every reasoning step and tool call is pushed live to the dashboard for full visibility.
- Default services now bind to `ws://127.0.0.1:18765` (scheduler) and `http://0.0.0.0:18888` (Web API / Dashboard).

### Breaking Changes
- Configuration moved to `~/.pyclaego/config.yaml` with `.config.d/` overlays; legacy single-file configs from 1.x require migration.
- SKILL-only execution paths are superseded by the ToolAgent runtime; integrations relying on in-loop SKILL semantics must be ported.
- Static HTML web UI under `web/static/` is deprecated in favor of the new Dashboard build.

## [1.0.0] - 2026-04-26

### Added
- Initial public release of PyClaego.
- Session-based AI agent framework with WebSocket scheduler.
- SoulV5 and SoulV6 context handlers with persistent memory.
- Pluggable LLM backends: Anthropic, OpenAI, Gemini, DeepSeek, Kimi.
- Built-in security executor with configurable rules.
- Feishu (Lark) message gateway.
- TUI client powered by Textual.
- FastAPI-based web server.
- Bootstrap script for one-time environment setup.
