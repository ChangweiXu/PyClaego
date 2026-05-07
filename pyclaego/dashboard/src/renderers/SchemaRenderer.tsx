/**
 * SchemaRenderer — walks a ViewSchema tree and renders the matching primitive.
 *
 * TypeScript's exhaustive `switch` on `schema.type` ensures all branches
 * are covered. A build error here means view_schema.py added a new type
 * without a matching case.
 *
 * Custom renderers (chat, markdown_editor) are registered in `customRenderers`
 * below. Everything else maps to one of the 9 generic primitives.
 */

import type { FC } from 'react';
import type { ViewSchema } from '../schema/types';
import type { TaskItem } from '../primitives/TaskList';

import { KVTable } from '../primitives/KVTable';
import { Stat } from '../primitives/Stat';
import { Markdown } from '../primitives/Markdown';
import { TaskList } from '../primitives/TaskList';
import { Tree } from '../primitives/Tree';
import { DocumentList } from '../primitives/DocumentList';
import { Toolbar } from '../primitives/Toolbar';
import { Stack } from '../primitives/Stack';
import { Split } from '../primitives/Split';
import { Tabs } from '../primitives/Tabs';

import { ChatRenderer } from './ChatRenderer';

// ---------------------------------------------------------------------------
// Custom renderer registry  (escape hatch)
// ---------------------------------------------------------------------------

const customRenderers: Record<string, FC<Record<string, unknown>>> = {
  // Register bespoke renderers here by name (matches schema.renderer string)
  // e.g.  markdown_editor: MarkdownEditorRenderer,
};

// ---------------------------------------------------------------------------
// Context injected by the drawer into all child renderers
// ---------------------------------------------------------------------------

export interface RendererContext {
  psId: string;
  widgetId: string;
  /** Fired when a Toolbar button or Tree node sends a command. */
  onCommand: (command: string, args?: Record<string, unknown>) => void;
  /** Task records for TaskList (managed by ChatRenderer or drawer). */
  tasks?: TaskItem[];
}

interface Props {
  schema: ViewSchema;
  ctx: RendererContext;
}

export function SchemaRenderer({ schema, ctx }: Props) {
  switch (schema.type) {
    // ---- Layout ----
    case 'split':
      return (
        <Split
          orientation={schema.orientation}
          ratio={schema.ratio}
          left={<SchemaRenderer schema={schema.left} ctx={ctx} />}
          right={<SchemaRenderer schema={schema.right} ctx={ctx} />}
        />
      );

    case 'tabs':
      return (
        <Tabs
          tabs={schema.tabs.map((t) => ({
            label: t.label,
            content: <SchemaRenderer schema={t.content} ctx={ctx} />,
          }))}
        />
      );

    case 'stack':
      return (
        <Stack gap={schema.gap}>
          {schema.children.map((child, i) => (
            <SchemaRenderer key={i} schema={child} ctx={ctx} />
          ))}
        </Stack>
      );

    case 'toolbar':
      return <Toolbar {...schema} onCommand={ctx.onCommand} />;

    // ---- Content ----
    case 'chat_log':
      // chat_log delegates to ChatRenderer which owns WS + task state
      return <ChatRenderer psId={ctx.psId} widgetId={ctx.widgetId} onCommand={ctx.onCommand} />;

    case 'task_list':
      return <TaskList {...schema} tasks={ctx.tasks} />;

    case 'tree':
      return <Tree {...schema} onCommand={ctx.onCommand} />;

    case 'markdown':
      return <Markdown {...schema} />;

    case 'document_list':
      return <DocumentList {...schema} />;

    case 'kv_table':
      return <KVTable {...schema} />;

    case 'stat':
      return <Stat {...schema} />;

    // ---- Escape hatch ----
    case 'custom': {
      const Renderer = customRenderers[schema.renderer];
      if (!Renderer) {
        return (
          <div className="sr-unknown">
            Unknown renderer: <code>{schema.renderer}</code>
          </div>
        );
      }
      return <Renderer {...schema.props} />;
    }

    default:
      // TypeScript exhaustiveness check — if this line has a type error,
      // a new schema type was added without a matching case above.
      return exhaustive(schema);
  }
}

function exhaustive(x: never): never {
  throw new Error(`SchemaRenderer: unhandled type ${(x as ViewSchema).type}`);
}
