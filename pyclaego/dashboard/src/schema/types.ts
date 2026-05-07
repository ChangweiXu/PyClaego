/**
 * ViewSchema — mirrors pyclaego/src/personal_space/view_schema.py
 *
 * IMPORTANT: Keep in sync with the Pydantic model.
 * Future: generate this file with `make types` (datamodel-code-generator).
 *
 * Design rules:
 *   - Content nodes reference data BY widget_id — never inline payloads.
 *   - No style/color/padding props in the schema.
 *   - `custom` is the escape hatch for bespoke React renderers.
 */

// ---------------------------------------------------------------------------
// Shared building blocks
// ---------------------------------------------------------------------------

export interface ButtonSpec {
  label: string;
  command: string;
  args?: Record<string, unknown>;
  variant?: 'primary' | 'danger' | 'ghost';
  disabled?: boolean;
}

export interface TreeNode {
  id: string;
  label: string;
  icon?: string;
  children?: TreeNode[];
  payload?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Content nodes
// ---------------------------------------------------------------------------

export interface KVTableSchema {
  type: 'kv_table';
  rows: [string, string][];
}

export interface StatSchema {
  type: 'stat';
  label: string;
  value: string | number;
  trend?: number;
}

export interface MarkdownSchema {
  type: 'markdown';
  text: string;
}

/** Messages live in the TanStack Query cache, keyed by widget_id. */
export interface ChatLogSchema {
  type: 'chat_log';
  widget_id: string;
}

/** Task list data lives in the cache; schema just declares the source widget. */
export interface TaskListSchema {
  type: 'task_list';
  widget_id: string;
  filter?: Record<string, unknown>;
}

export interface TreeSchema {
  type: 'tree';
  nodes: TreeNode[];
  on_select_command?: string;
}

export interface DocumentListSchema {
  type: 'document_list';
  doc_ids: string[];
}

// ---------------------------------------------------------------------------
// Layout nodes
// ---------------------------------------------------------------------------

export interface ToolbarSchema {
  type: 'toolbar';
  buttons: ButtonSpec[];
}

export interface SplitSchema {
  type: 'split';
  orientation: 'h' | 'v';
  left: ViewSchema;
  right: ViewSchema;
  ratio?: number;
}

export interface TabItem {
  label: string;
  content: ViewSchema;
}

export interface TabsSchema {
  type: 'tabs';
  tabs: TabItem[];
}

export interface StackSchema {
  type: 'stack';
  gap?: number;
  children: ViewSchema[];
}

// ---------------------------------------------------------------------------
// Escape hatch
// ---------------------------------------------------------------------------

export interface CustomSchema {
  type: 'custom';
  renderer: string;
  props: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Discriminated union
// ---------------------------------------------------------------------------

export type ViewSchema =
  // Layout
  | SplitSchema
  | TabsSchema
  | StackSchema
  | ToolbarSchema
  // Content
  | ChatLogSchema
  | TaskListSchema
  | TreeSchema
  | MarkdownSchema
  | DocumentListSchema
  | KVTableSchema
  | StatSchema
  // Escape hatch
  | CustomSchema;

// ---------------------------------------------------------------------------
// Command
// ---------------------------------------------------------------------------

export interface WidgetCommand {
  command: string;
  args?: Record<string, unknown>;
}

export interface CommandResult {
  ok: boolean;
  data?: Record<string, unknown>;
  error?: string;
}
