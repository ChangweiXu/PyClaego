# Dashboard — Development Conventions

This document codifies the design principles that govern the dashboard codebase.
Follow them when adding features, schema types, or new primitives.

---

## 1. Schema-driven rendering

### The core rule
**The server owns layout and content structure. The frontend owns only rendering.**

Every widget's drawer body is described by a `ViewSchema` tree returned from
`GET /api/v2/personal_spaces/:ps/widgets/:id/view`. The frontend never decides
"this widget class should show a chat log on the left and a task list on the right" —
that decision lives in the backend `WidgetClass.get_view()` implementation.

### What belongs in a schema node
- Content: `text`, `rows`, `nodes`, `doc_ids`, `widget_id` references
- Structure: `type`, `orientation`, `ratio`, `tabs`, `children`
- Commands: `command`, `args` on buttons and tree nodes

### What does NOT belong in a schema node
- Colors, padding, font sizes — these live in `src/styles.css` under the
  primitive's BEM class (`p-toolbar`, `p-stat`, etc.)
- Business logic or conditional display — derive these from content, not style props
- Inline data payloads for live content — use `widget_id` references so TanStack
  Query owns caching and invalidation

---

## 2. Adding a new schema type

Follow these steps in order. A missed step causes a TypeScript build error in
`SchemaRenderer`, which is intentional.

### Step 1 — `src/schema/types.ts`
Add an interface and include it in the `ViewSchema` union:
```ts
export interface MyNodeSchema {
  type: 'my_node';
  // ... fields (no style props)
}

export type ViewSchema =
  | ...existing types...
  | MyNodeSchema;
```

### Step 2 — `src/primitives/MyNode.tsx`
Create the primitive. Accept the schema interface directly as props:
```tsx
import type { MyNodeSchema } from '../schema/types';

export function MyNode({ field1, field2 }: MyNodeSchema) {
  return <div className="p-mynode">…</div>;
}
```

Add CSS for `.p-mynode*` to `src/styles.css` — no inline styles.

### Step 3 — `src/renderers/SchemaRenderer.tsx`
Add a `case` in the `switch`. The `exhaustive()` guard will produce a build error
until you do:
```ts
case 'my_node':
  return <MyNode {...schema} />;
```

If the node needs access to `psId`, `widgetId`, or `onCommand`, read them from
`ctx` rather than adding them to the schema:
```ts
case 'my_node':
  return <MyNode {...schema} onCommand={ctx.onCommand} />;
```

### Step 4 — Sync the backend
Update `pyclaego/src/personal_space/view_schema.py` with the matching Pydantic
model. The comment at the top of `schema/types.ts` explains the sync requirement.

---

## 3. Custom renderers (escape hatch)

Use `custom` schema nodes only when a primitive genuinely cannot express the
interaction (e.g. a rich text editor, a canvas, a media player).

Register in `SchemaRenderer.tsx`:
```ts
const customRenderers: Record<string, FC<Record<string, unknown>>> = {
  my_renderer: MyRendererComponent,
};
```

The backend emits `{ type: 'custom', renderer: 'my_renderer', props: { … } }`.
Props are `Record<string, unknown>` — type-narrow inside the component.

`ChatRenderer` is the canonical example: it is registered implicitly by the
`case 'chat_log'` branch (not via the registry) because it owns complex local
state (WS subscription, message history, task records).

---

## 4. State layer rules

Each store has an exclusive ownership domain. Never write to a store that doesn't
own the data.

| Store | Owner | Contains | Written by |
|---|---|---|---|
| TanStack Query cache | REST responses | `widgets`, `highlight`, `view`, `info`, task artifacts | `useQuery` / `queryClient.setQueryData` |
| `useLiveStore` | WS streaming | Chat messages, streaming chunks, per-widget task records | `ws/bridge.ts` dispatch |
| `useTasksStore` | Task tree | Full session → task tree | `ws/tasksWS.ts` + `useTasks` REST fallback |
| `useUIStore` | Client UI | Drawer open/close flags, selected task ID | Components |
| `useDraftsStore` | Persisted input | Unsent chat drafts (localStorage) | `ChatRenderer` |

### WS → cache update pattern
When the WS delivers a status or view change, write to the TanStack Query cache
directly (not to a Zustand store):
```ts
queryClient.setQueryData(['widget', psId, widgetId, 'highlight'], newHighlight);
```
This keeps component subscriptions via `useWidgetHighlight()` valid without
an extra layer of reactivity.

### REST fallback policy
The `useTasks` query guard (`if (tasksWS.connected) return`) prevents the REST
poll from overwriting incremental WS patches. Apply the same pattern if adding a
new resource that has both a WS push path and a REST poll.

---

## 5. TanStack Query cache key convention

All widget-related keys follow a four-segment tuple:

```
['widget', psId, widgetId, sub-resource]
```

Sub-resource values: `'info'`, `'highlight'`, `'view'`

The widgets list uses:
```
['widgets', psId]
```

Do **not** invent ad-hoc key shapes — consistent keys make `invalidateQueries`
and `setQueryData` predictable.

---

## 6. WS bridge rules

`ws/bridge.ts` is a **singleton** — one persistent connection for the app
lifetime. Do not create per-component WebSocket connections for the chat channel.

Rules:
- Call `bridge.start()` exactly once at app boot (in `App.tsx`).
- Call `bridge.ensurePSOpen(psId)` whenever entering a PS route — it is idempotent.
- Subscribe to replies via `bridge.onReply(cb)` and always return the unsubscribe
  function from `useEffect`.
- The bridge auto-reconnects and replays `open` for all seen PSes on reconnect.

`ws/tasksWS.ts` is a separate singleton for `/ws/tasks` (different protocol:
chunked snapshots + incremental patches). Do not merge with the chat bridge.

---

## 7. WidgetCard highlight contract

`WidgetCard` reads named keys from the server-produced `highlight` dict:

| Key | Type | Meaning |
|---|---|---|
| `status` | `'idle' \| 'working' \| 'busy' \| 'error'` | Drives the status dot |
| `busy` | `boolean` | Overrides status to `working` (WS live flag) |
| `current_question` | `string` | Short preview shown while working |
| `llm` | `string` | LLM model badge |
| `agent_type` | `string` | Agent class badge |
| `context_type` | `string` | Context class badge |
| `msg_count` | `number` | Message count badge |

**TODO:** Formalise this as a `HighlightSchema` interface in `schema/types.ts` so
the contract is type-checked rather than implicitly documented here.

---

## 8. Primitive component rules

- Primitives live in `src/primitives/`. One file per schema node type.
- Props extend or match the corresponding `*Schema` interface from `schema/types.ts`.
- Primitives are **pure presentational** — no data fetching, no store subscriptions,
  no direct WS access.
- If a primitive needs runtime data not carried in the schema (e.g. `resolveDoc`
  for `DocumentList`), receive it as an optional callback prop injected by
  `SchemaRenderer` via `RendererContext`. Never bypass this through a hook inside
  the primitive.
- CSS class prefix: `p-<node-type>` (e.g. `p-kv-table`, `p-stat`, `p-toolbar`).

---

## 9. `RendererContext` extension pattern

When a new primitive needs server-provided runtime data, extend `RendererContext`
in `SchemaRenderer.tsx` and populate it in `WidgetDrawer.tsx`:

```ts
// SchemaRenderer.tsx
export interface RendererContext {
  psId: string;
  widgetId: string;
  onCommand: (command: string, args?: Record<string, unknown>) => void;
  tasks?: TaskItem[];
  resolveDoc?: (docId: string) => string | undefined;  // ← example extension
}
```

`WidgetDrawer` is the single source that builds and passes `ctx`. Keep context
construction there — do not let nested components build partial contexts.

---

## 10. What NOT to do

| Anti-pattern | Correct approach |
|---|---|
| Import a store inside a primitive | Pass data as props via `RendererContext` |
| Add a `color` or `padding` prop to a schema node | Style via CSS class on the primitive |
| Create a component for a single widget class | Use `custom` schema node + named renderer |
| Fetch data directly inside `SchemaRenderer` | Fetch in `WidgetDrawer`, pass via `ctx` |
| Add a new WS connection per component | Use `bridge` / `tasksWS` singletons |
| Write server cache data to Zustand | Write to `queryClient.setQueryData` |
| Skip the `exhaustive()` check | Add the `case` or the build fails |
