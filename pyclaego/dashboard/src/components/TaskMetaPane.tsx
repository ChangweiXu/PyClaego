/**
 * TaskMetaPane — bottom-right detail panel in the task tree drawer.
 *
 * Displays metadata JSON (with popup viewer) and artifacts list.
 */

import { useState } from 'react';
import type { TaskNode, ArtifactRef } from '../api';
import { useTaskArtifacts } from '../queries/tasks';
import ArtifactViewer from './ArtifactViewer';
import JsonModal from './JsonModal';
import { api } from '../api';

// ─── Artifact row ───────────────────────────────────────────────────────

function ArtifactRow({ taskId, artifact }: { taskId: string; artifact: ArtifactRef }) {
  const [open, setOpen] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [modalContent, setModalContent] = useState<string>('');
  const [modalLoading, setModalLoading] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const openModal = async () => {
    setModalOpen(true);
    setModalLoading(true);
    setModalError(null);
    try {
      const { text, mime } = await api.getTaskArtifactBlob(taskId, artifact.artifact_id);
      let body = text;
      if (mime.includes('json')) {
        try { body = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ }
      }
      setModalContent(body);
    } catch (e) {
      setModalError(String(e));
    } finally {
      setModalLoading(false);
    }
  };

  return (
    <div className="artifact-row-wrap">
      <div className="artifact-row">
        <span className="artifact-kind-tag">{artifact.kind}</span>
        <span className="artifact-name">{artifact.name}</span>
        <span className="artifact-size">
          {artifact.size > 1024 ? `${(artifact.size / 1024).toFixed(1)}KB` : `${artifact.size}B`}
        </span>
        <button
          className="artifact-view-btn"
          onClick={openModal}
          title="Open in popup"
        >
          view
        </button>
        <button
          className={`artifact-view-btn${open ? ' artifact-view-btn--open' : ''}`}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? 'hide' : 'show'}
        </button>
      </div>
      {open && <ArtifactViewer taskId={taskId} artifact={artifact} />}
      <JsonModal
        open={modalOpen}
        title={`${artifact.name} · ${artifact.mime}`}
        json={modalLoading ? 'Loading…' : (modalError ? `Error: ${modalError}` : modalContent)}
        onClose={() => setModalOpen(false)}
      />
    </div>
  );
}

// ─── Main export ────────────────────────────────────────────────────────

export function TaskMetaPane({ task }: { task: TaskNode | null }) {
  const { data, isLoading } = useTaskArtifacts(task?.task_id ?? null);
  const artifacts = data?.artifacts ?? [];
  const [metaModalOpen, setMetaModalOpen] = useState(false);

  if (!task) return <div className="task-detail-empty" />;

  const metaDisplay = Object.entries(task.metadata ?? {}).filter(([k]) => k !== 'digest');
  const metaObject = Object.fromEntries(metaDisplay);

  return (
    <div className="task-detail-pane">
      {metaDisplay.length > 0 && (
        <div className="task-detail-section">
          <div className="task-detail-section-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>Metadata</span>
            <button
              className="artifact-view-btn"
              onClick={() => setMetaModalOpen(true)}
              title="Open in popup"
            >
              view
            </button>
          </div>
          <pre className="task-detail-pre">
            {JSON.stringify(metaObject, null, 2)}
          </pre>
          <JsonModal
            open={metaModalOpen}
            title="Metadata"
            json={metaObject}
            onClose={() => setMetaModalOpen(false)}
          />
        </div>
      )}

      <div className="task-detail-section">
        <div className="task-detail-section-title">
          Artifacts
          {!isLoading && <span className="tasks-count-badge" style={{ marginLeft: 6 }}>{artifacts.length}</span>}
          {isLoading && <span className="task-detail-loading-dot">…</span>}
        </div>
        {!isLoading && artifacts.length === 0 && (
          <div className="task-detail-empty-inner">(none)</div>
        )}
        {artifacts.map((a) => (
          <ArtifactRow key={a.artifact_id} taskId={task.task_id} artifact={a} />
        ))}
      </div>
    </div>
  );
}
