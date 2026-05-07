/**
 * tasksStore — global task tree data pushed from /ws/tasks and /api/tasks/.
 *
 * Keyed by session_id; values are root TaskNode arrays (children nested).
 * Both the tasksWS singleton and the useTasks() REST query write here.
 */
import { create } from 'zustand';
import type { TaskNode } from '../api';

/** Flat task snapshot as stored in the bridge cache (no children array). */
export interface RawTaskSnapshot {
  task_id: string;
  parent_id?: string | null;
  name: string;
  task_type: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  metadata: Record<string, unknown>;
  description: string;
  result?: unknown;
  error?: string | null;
}

function buildNode(task: RawTaskSnapshot, flat: Record<string, RawTaskSnapshot>): TaskNode {
  const children: TaskNode[] = [];
  for (const t of Object.values(flat)) {
    if (t.parent_id === task.task_id) {
      children.push(buildNode(t, flat));
    }
  }
  return {
    task_id: task.task_id,
    name: task.name,
    task_type: task.task_type,
    status: task.status,
    progress: task.progress,
    created_at: task.created_at,
    started_at: task.started_at,
    finished_at: task.finished_at,
    metadata: task.metadata,
    description: task.description,
    result: task.result,
    error: task.error,
    children,
  };
}

/** Build a nested TaskNode tree from a flat {task_id → snapshot} dict. */
export function buildTreeFromFlat(flat: Record<string, RawTaskSnapshot>): TaskNode[] {
  return Object.values(flat)
    .filter((t) => !t.parent_id)
    .map((t) => buildNode(t, flat));
}

interface TasksState {
  /** Full task tree keyed by session_id. */
  sessions: Record<string, TaskNode[]>;
  /** Internal flat cache for incremental patching. */
  _flatCache: Record<string, Record<string, RawTaskSnapshot>>;
  /** Replace entire snapshot (REST poll fallback). */
  setSnapshot: (tree: Record<string, TaskNode[]>) => void;
  /** Commit buffered snapshot chunks (WS snapshot_done). */
  setFlatSnapshot: (flatAll: Record<string, Record<string, RawTaskSnapshot>>) => void;
  /** Apply incremental patch for one session (WS task_update). */
  patchSession: (sessionId: string, flatPatch: Record<string, RawTaskSnapshot>) => void;
}

export const useTasksStore = create<TasksState>((set, get) => ({
  sessions: {},
  _flatCache: {},
  setSnapshot: (tree) => set({ sessions: tree }),
  setFlatSnapshot: (flatAll) => {
    const sessions: Record<string, TaskNode[]> = {};
    for (const [sid, flat] of Object.entries(flatAll)) {
      sessions[sid] = buildTreeFromFlat(flat);
    }
    set({ sessions, _flatCache: flatAll });
  },
  patchSession: (sessionId, flatPatch) => {
    const { _flatCache, sessions } = get();
    const updated = { ...(_flatCache[sessionId] ?? {}), ...flatPatch };
    set({
      sessions: { ...sessions, [sessionId]: buildTreeFromFlat(updated) },
      _flatCache: { ..._flatCache, [sessionId]: updated },
    });
  },
}));
