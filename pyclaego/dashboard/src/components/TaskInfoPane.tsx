/**
 * TaskInfoPane — bottom-left detail panel in the task tree drawer.
 *
 * Displays task header (icon + name + status), info fields (task_id, type, error),
 * timing (created/started/finished/duration) and description.
 */

import type { ReactNode } from 'react';
import type { TaskNode } from '../api';

// ─── Shared constants ──────────────────────────────────────────────────────

export const TYPE_ICON: Record<string, string> = {
  user_message:   '💬',
  agent_loop:     '🔁',
  tool_execution: '🔧',
  subagent_spawn: '🧬',
  subagent_loop:  '🔄',
  llm_call:       '🧠',
  memory_read:    '📖',
  memory_write:   '💾',
  memory_search:  '🔍',
};

export const STATUS_COLOR: Record<string, string> = {
  pending:   'tasks-status-badge--pending',
  running:   'tasks-status-badge--running',
  completed: 'tasks-status-badge--completed',
  failed:    'tasks-status-badge--failed',
  cancelled: 'tasks-status-badge--cancelled',
};

// ─── Helpers ──────────────────────────────────────────────────────────────

function fmt(ts: string | null | undefined): string {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}

function dur(ms: unknown): string {
  if (typeof ms !== 'number') return '—';
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`;
}

// ─── Field row ────────────────────────────────────────────────────────────

export function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="task-detail-field-row">
      <span className="task-detail-field-label">{label}</span>
      <span className="task-detail-field-value">{value}</span>
    </div>
  );
}

// ─── Main export ──────────────────────────────────────────────────────────

export function TaskInfoPane({ task }: { task: TaskNode | null }) {
  if (!task) {
    return <div className="task-detail-empty"><span>← Select a task node</span></div>;
  }
  const digest = (task.metadata?.digest ?? {}) as Record<string, unknown>;
  return (
    <div className="task-detail-pane">
      <div className="task-detail-header">
        <span className="task-detail-header-icon">{TYPE_ICON[task.task_type] ?? '⚙️'}</span>
        <span className="task-detail-header-name">{task.name || task.task_type}</span>
        <span className={`tasks-status-badge ${STATUS_COLOR[task.status] ?? ''}`}>{task.status}</span>
      </div>

      <div className="task-detail-section">
        <div className="task-detail-section-title">Info</div>
        <Field label="task_id" value={<span style={{ fontFamily: 'monospace', fontSize: '0.75rem' }}>{task.task_id}</span>} />
        <Field label="type" value={task.task_type} />
        {task.error && <div className="task-detail-error">{task.error}</div>}
      </div>

      <div className="task-detail-section">
        <div className="task-detail-section-title">Timing</div>
        <Field label="created"  value={fmt(task.created_at)} />
        <Field label="started"  value={fmt(task.started_at)} />
        <Field label="finished" value={fmt(task.finished_at)} />
        <Field label="duration" value={dur(digest.duration_ms)} />
      </div>

      {task.description && (
        <div className="task-detail-section">
          <div className="task-detail-section-title">Description</div>
          <pre className="task-detail-pre">{task.description}</pre>
        </div>
      )}
    </div>
  );
}
