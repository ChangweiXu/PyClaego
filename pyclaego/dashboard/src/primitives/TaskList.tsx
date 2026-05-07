import type { TaskListSchema } from '../schema/types';
import { useWidgetInfo } from '../queries/widgets';

export type TaskStatus = 'pending' | 'running' | 'done' | 'error' | 'cancelled';

export interface TaskItem {
  id: string;
  label: string;
  status: TaskStatus;
  startedAt?: string;
  duration?: number; // ms
  error?: string;
}

// TaskList reads live task data from props (pushed via WS bridge into parent state).
// When used inside SchemaRenderer it receives taskItems from the nearest ChatRenderer
// or from the TanStack Query task cache once that's wired up.

interface TaskListProps extends TaskListSchema {
  /** Injected by the surrounding renderer. */
  tasks?: TaskItem[];
  /** Callback when user clicks the detail button on a task item. */
  onTaskDetail?: (taskId: string) => void;
  /** Callback when user clicks the Stream button on a task item. */
  onTaskStream?: (taskId: string) => void;
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: 'Pending',
  running: 'Running',
  done: 'Done',
  error: 'Error',
  cancelled: 'Cancelled',
};

export function TaskList({ tasks = [], onTaskDetail, onTaskStream }: TaskListProps) {
  if (!tasks.length) {
    return <div className="p-tasklist-empty">No task records yet.</div>;
  }
  return (
    <ol className="p-tasklist">
      {tasks.map((t) => (
        <li key={t.id} className={`p-task p-task-${t.status}`}>
          <span className={`p-task-badge ${t.status}`}>{STATUS_LABEL[t.status]}</span>
          <button
            className="p-task-stream-btn"
            onClick={(e) => { e.stopPropagation(); onTaskStream?.(t.id); }}
            title="View stream record"
          >
            Stream
          </button>
          <span className="p-task-label">{t.label}</span>
          {t.duration !== undefined && (
            <span className="p-task-dur">{(t.duration / 1000).toFixed(1)}s</span>
          )}
          {t.error && <span className="p-task-error">{t.error}</span>}
        </li>
      ))}
    </ol>
  );
}
