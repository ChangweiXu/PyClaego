/**
 * TaskDetailPane — right-side detail panel in the Tasks drawer.
 *
 * Displays fields, metadata digest, error, metadata JSON, and artifacts
 * for the currently selected task node.
 */
import { useState } from 'react';
import type { TaskNode, ArtifactRef } from '../api';
import { useTaskArtifacts } from '../queries/tasks';
import ArtifactViewer from './ArtifactViewer';

const TYPE_ICON: Record<string, string> = {
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

const STATUS_COLOR: Record<string, string> = {
  pending:   'tasks-status-badge--pending',
  running:   'tasks-status-badge--running',
  completed: 'tasks-status-badge--completed',
  failed:    'tasks-status-badge--failed',
  cancelled: 'tasks-status-badge--cancelled',
};

function formatTs(ts: string | null): string {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}

function formatDuration(ms: unknown): string {
  if (typeof ms !== 'number') return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// ─── Field row ────────────────────────────────────────────────────────────
function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="task-detail-field-row">
      <span className="task-detail-field-label">{label}</span>
      <span className="task-detail-field-value">{value}</span>
    </div>
  );
}

// ─── Copy-to-clipboard ID button ─────────────────────────────────────────
function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(id).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = id;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    });
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  return (
    <button className="task-detail-id-copy" onClick={copy} title="Copy task_id">
      {copied ? '✓' : '⧉'}
    </button>
  );
}

// ─── Artifact row with toggle viewer ──────────────────────────────────────
function ArtifactRow({ taskId, artifact }: { taskId: string; artifact: ArtifactRef }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="artifact-row-wrap">
      <div className="artifact-row">
        <span className="artifact-kind-tag">{artifact.kind}</span>
        <span className="artifact-name">{artifact.name}</span>
        <span className="artifact-size">{artifact.size > 1024
          ? `${(artifact.size / 1024).toFixed(1)}KB`
          : `${artifact.size}B`}
        </span>
        <button
          className={`artifact-view-btn${open ? ' artifact-view-btn--open' : ''}`}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? 'hide' : 'view'}
        </button>
      </div>
      {open && <ArtifactViewer taskId={taskId} artifact={artifact} />}
    </div>
  );
}

// ─── Artifacts section ────────────────────────────────────────────────────
function ArtifactsSection({ taskId }: { taskId: string }) {
  const { data, isLoading, error } = useTaskArtifacts(taskId);
  const artifacts = data?.artifacts ?? [];

  return (
    <div className="task-detail-section">
      <div className="task-detail-section-title">
        Artifacts
        {!isLoading && (
          <span className="tasks-count-badge" style={{ marginLeft: 6 }}>{artifacts.length}</span>
        )}
        {isLoading && <span className="task-detail-loading-dot">…</span>}
      </div>
      {error && <div className="task-detail-error">Failed to load artifacts</div>}
      {!isLoading && !error && artifacts.length === 0 && (
        <div className="task-detail-empty-inner">(none)</div>
      )}
      {artifacts.map((a) => (
        <ArtifactRow key={a.artifact_id} taskId={taskId} artifact={a} />
      ))}
    </div>
  );
}

// ─── Main export ──────────────────────────────────────────────────────────
export default function TaskDetailPane({ task }: { task: TaskNode | null }) {
  if (!task) {
    return (
      <div className="task-detail-empty">
        <span>←&nbsp;Select a task node</span>
      </div>
    );
  }

  const digest = (task.metadata?.digest ?? {}) as Record<string, unknown>;
  const durationMs = digest.duration_ms;
  // Metadata minus digest for display
  const metaDisplay = Object.entries(task.metadata ?? {}).filter(([k]) => k !== 'digest');

  return (
    <div className="task-detail-pane">
      {/* Header */}
      <div className="task-detail-header">
        <span className="task-detail-header-icon">{TYPE_ICON[task.task_type] ?? '⚙️'}</span>
        <span className="task-detail-header-name">{task.name || task.task_type}</span>
        <span className={`tasks-status-badge ${STATUS_COLOR[task.status] ?? ''}`}>
          {task.status}
        </span>
      </div>

      {/* Fields */}
      <div className="task-detail-section">
        <div className="task-detail-section-title">Info</div>
        <Field label="task_id" value={<span style={{ fontFamily: 'monospace', fontSize: '0.75rem' }}>{task.task_id}<CopyId id={task.task_id} /></span>} />
        <Field label="type" value={task.task_type} />
        {task.error && (
          <div className="task-detail-error">{task.error}</div>
        )}
      </div>

      {/* Timestamps */}
      <div className="task-detail-section">
        <div className="task-detail-section-title">Timing</div>
        <Field label="created" value={formatTs(task.created_at)} />
        <Field label="started" value={formatTs(task.started_at)} />
        <Field label="finished" value={formatTs(task.finished_at)} />
        <Field label="duration" value={formatDuration(durationMs)} />
      </div>

      {/* Description */}
      {task.description && (
        <div className="task-detail-section">
          <div className="task-detail-section-title">Description</div>
          <pre className="task-detail-pre">{task.description}</pre>
        </div>
      )}

      {/* Metadata (excluding digest) */}
      {metaDisplay.length > 0 && (
        <div className="task-detail-section">
          <div className="task-detail-section-title">Metadata</div>
          <pre className="task-detail-pre">
            {JSON.stringify(Object.fromEntries(metaDisplay), null, 2)}
          </pre>
        </div>
      )}

      {/* Artifacts */}
      <ArtifactsSection taskId={task.task_id} />
    </div>
  );
}
