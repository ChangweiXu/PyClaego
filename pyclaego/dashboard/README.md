# PyClaego Dashboard

React + Vite SPA for managing PersonalSpaces and their widgets.  
Architecture is **schema-driven**: every widget's drawer body is described by a
server-produced `ViewSchema` tree; the frontend never hard-codes widget-specific
layouts. See [`README-CONVENTION.md`](./README-CONVENTION.md) for dev principles.

## Dev

```bash
cd pyclaego/dashboard
npm install
npm run dev          # http://localhost:5173/dashboard/
```

Vite proxies `/api/*` and `/ws/*` to `http://127.0.0.1:8000` — start the
backend separately (`pyclaego/scripts/start_core.sh` + the web server).

## Production build

```bash
npm run build
```

Produces `dist/`, which `pyclaego/src/web/app.py` mounts at `/dashboard`
automatically.  After building, visit `http://<web-host>:8000/dashboard`.

## Routes

| Path | Description |
|---|---|
| `/` | Redirects to first PS (or empty state) |
| `/:psId` | Bento grid of widget cards + slide-in drawer |
| `/tasks` | Full-page task browser (PS → Widget → Tree → Detail) |

## Architecture layers

```
src/
├── schema/types.ts        ViewSchema discriminated union (mirrors backend Pydantic)
├── renderers/
│   ├── SchemaRenderer.tsx  exhaustive switch → primitives/custom renderers
│   └── ChatRenderer.tsx    custom renderer for chat_log nodes (WS + task state)
├── primitives/             one component per schema node type, typed via schema
├── components/             composed UI (WidgetCard, WidgetDrawer, AddWidgetModal, …)
├── pages/                  Dashboard, TasksPage
├── queries/                TanStack Query hooks (cache key conventions in widgets.ts)
├── store/                  Zustand stores — live.ts · tasks.ts · ui.ts · drafts.ts
└── ws/                     bridge.ts (chat WS) · tasksWS.ts (task WS)
```

### Data flow for a widget drawer

```
GET /api/v2/…/view  →  ViewSchema
        ↓
  SchemaRenderer (switch on schema.type)
        ↓
  primitive  ──or──  ChatRenderer (chat_log)
                          ↓
                    WSBridge (streaming replies)
                    useLiveStore (messages, tasks)
```

## Backend contracts

| Channel | Path | Notes |
|---|---|---|
| REST | `/api/v2/*` | `src/api.ts` |
| Chat WS | `/ws/v2/chat` | `open / chat / control` messages — `src/ws/bridge.ts` |
| Tasks WS | `/ws/tasks` | `snapshot_chunk / snapshot_done / task_update` — `src/ws/tasksWS.ts` |

## State layers

| Layer | Store / hook | What lives here |
|---|---|---|
| Server cache | TanStack Query | REST responses (widgets, highlight, view, task artifacts) |
| Live ephemeral | `useLiveStore` | Streaming chunks, chat messages, per-widget task records |
| Global task tree | `useTasksStore` | Full task tree from `/ws/tasks` + REST fallback |
| UI state | `useUIStore` | Drawer open/close, selected task ID |
| Persisted drafts | `useDraftsStore` | Unsent chat input (localStorage) |

## Known gaps / TODOs

- **`DocumentList.resolveDoc` not wired** — `SchemaRenderer` spreads schema props
  but never injects `resolveDoc`; documents always show "Loading…" in non-custom
  renderer paths. A `resolveDoc` factory needs to be added to `RendererContext`.
- **`TaskList` outside `chat_log` shows empty** — `RendererContext.tasks` is never
  populated by `WidgetDrawer`; standalone `task_list` schema nodes require the
  drawer to subscribe the global `useTasksStore` and inject the matching slice.
- **`HighlightSchema` type missing** — `WidgetCard` reads `highlight` fields
  (`agent_type`, `llm`, `msg_count`, `current_question`) via untyped
  `Record<string, unknown>`. A `HighlightSchema` interface in `schema/types.ts`
  would make this contract explicit.
- **`AddWidgetModal` config form is hand-coded** — the modal manually renders
  `llm` / `agent` / `context` fields instead of driving the form from the
  backend `config_schema` (JSON Schema). Migrating to `@rjsf/core` would make
  any new widget class automatically configurable without frontend changes.
