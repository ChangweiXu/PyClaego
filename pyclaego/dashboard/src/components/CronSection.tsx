/**
 * CronSection — cron trigger list shown at the bottom of the chat-tasks-panel.
 *
 * Features:
 *  - Lists all cron triggers for the current widget
 *  - "+" button to create a new trigger
 *  - "Edit" button per trigger opens CronEditBubble
 *  - "Delete" button per trigger removes it immediately
 *  - Save in bubble → full array PUT via useUpdateWidgetCron
 */

import { useState } from 'react';
import type { WidgetCronTrigger } from '../api';
import { useWidgetCron, useUpdateWidgetCron } from '../queries/cron';
import CronEditBubble from './CronEditBubble';

interface Props {
  psId: string;
  widgetId: string;
}

interface EditState {
  cron: WidgetCronTrigger | null;
  isNew: boolean;
  index: number; // -1 for new
}

export default function CronSection({ psId, widgetId }: Props) {
  const { data, isLoading } = useWidgetCron(psId, widgetId);
  const { mutate: updateCron, isPending } = useUpdateWidgetCron(psId, widgetId);

  const [editing, setEditing] = useState<EditState | null>(null);

  const crons: WidgetCronTrigger[] = data?.cron ?? [];

  const handleEdit = (cron: WidgetCronTrigger, index: number) => {
    setEditing({ cron, isNew: false, index });
  };

  const handleNew = () => {
    setEditing({ cron: null, isNew: true, index: -1 });
  };

  const handleDelete = (index: number) => {
    const next = crons.filter((_, i) => i !== index);
    updateCron(next);
  };

  const handleSave = (saved: WidgetCronTrigger) => {
    if (!editing) return;
    let next: WidgetCronTrigger[];
    if (editing.isNew) {
      next = [...crons, saved];
    } else {
      next = crons.map((c, i) => (i === editing.index ? saved : c));
    }
    updateCron(next, { onSuccess: () => setEditing(null) });
  };

  return (
    <div className="cron-section">
      <div className="cron-section-header">
        <span className="cron-section-title">Cron</span>
        <button
          className="cron-add-btn"
          onClick={handleNew}
          title="Add cron trigger"
          disabled={isPending}
        >
          ＋
        </button>
      </div>

      {isLoading && <div className="cron-loading">Loading…</div>}

      {!isLoading && crons.length === 0 && (
        <div className="cron-empty">No schedules yet</div>
      )}

      {crons.map((c, i) => (
        <div key={c.id ?? i} className="cron-row">
          <div className="cron-row-info">
            <span className="cron-row-id">{c.id}</span>
            <span className="cron-row-schedule">
              {c.schedule ?? (c.interval_seconds != null ? `every ${c.interval_seconds}s` : '—')}
            </span>
            <span className="cron-row-prompt" title={c.prompt}>
              {c.prompt.length > 30 ? c.prompt.slice(0, 30) + '…' : c.prompt}
            </span>
          </div>
          <div className="cron-row-badges">
            <span className={`cron-enabled-badge${c.enabled === false ? ' disabled' : ''}`}>
              {c.enabled === false ? 'off' : 'on'}
            </span>
          </div>
          <div className="cron-row-actions">
            <button
              className="cron-row-btn"
              onClick={() => handleEdit(c, i)}
              disabled={isPending}
            >
              Edit
            </button>
            <button
              className="cron-row-btn cron-row-btn--danger"
              onClick={() => handleDelete(i)}
              disabled={isPending}
              title="Delete this trigger"
            >
              ✕
            </button>
          </div>
        </div>
      ))}

      {editing && (
        <CronEditBubble
          cron={editing.cron}
          isNew={editing.isNew}
          onSave={handleSave}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
