/**
 * TanStack Query hooks for the global tasks API.
 *
 * Cache key: ['tasks']
 * Polled at 5s as REST fallback; the tasksWS singleton patches the
 * Zustand tasks store directly and is the primary update mechanism.
 * The REST poll only writes to the store when the WS is disconnected,
 * so it never clobbers incremental WS patches or the flat cache.
 */
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { api } from '../api';
import type { TaskNode, AgentNode } from '../api';
import { useTasksStore } from '../store/tasks';
import { tasksWS } from '../ws/tasksWS';
import type { ChatTaskItem } from '../store/live';

export function useTasks() {
  const setSnapshot = useTasksStore((s) => s.setSnapshot);

  const query = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.getTasks(),
    refetchInterval: 5_000,
  });

  // Only write to the store from REST when the WS is disconnected.
  // While WS is live, the chunked protocol maintains _flatCache — a
  // blind setSnapshot would leave _flatCache stale and cause the next
  // patchSession call to rebuild a session from an incomplete flat base.
  useEffect(() => {
    if (!query.data?.sessions) return;
    if (tasksWS.connected) return;

    // Normalize REST shape { session_id: { root_tasks: [...] } } → { session_id: [...] }
    const normalized: Record<string, import('../api').TaskNode[]> = {};
    for (const [sid, val] of Object.entries(query.data.sessions)) {
      normalized[sid] = Array.isArray(val) ? val : (val as { root_tasks?: import('../api').TaskNode[] }).root_tasks ?? [];
    }
    setSnapshot(normalized);
  }, [query.data, setSnapshot]);

  return query;
}

export function useTaskArtifacts(taskId: string | null) {
  return useQuery({
    queryKey: ['tasks', 'artifacts', taskId],
    queryFn: () => api.getTaskArtifacts(taskId!),
    enabled: !!taskId,
    staleTime: 30_000,
  });
}

// Stable empty reference so hook callers never get a new array identity on miss.
const _EMPTY_NODES: import('../api').TaskNode[] = [];

function _mapTaskStatus(s: string): ChatTaskItem['status'] {
  if (s === 'completed') return 'done';
  if (s === 'failed') return 'error';
  return s as ChatTaskItem['status'];
}

/**
 * Returns the task record list for a specific widget, sourced from the global
 * tasks store (populated by tasksWS + REST poll). Re-renders whenever the
 * backend emits a task_update for this widget's session key.
 *
 * Only root tasks with task_type === 'user_message' are included, sorted
 * chronologically.
 */
export function useWidgetTasks(psId: string, widgetId: string): ChatTaskItem[] {
  const sessionKey = `${psId}__${widgetId}`;
  const rootTasks = useTasksStore((s) => s.sessions[sessionKey] ?? _EMPTY_NODES);

  return useMemo(() => {
    return [...rootTasks]
      .filter((t) => t.task_type === 'user_message')
      .sort((a, b) => {
        const ta = a.created_at ?? '';
        const tb = b.created_at ?? '';
        return ta < tb ? -1 : ta > tb ? 1 : 0;
      })
      .map((t): ChatTaskItem => {
        const digest = (t.metadata?.digest ?? {}) as Record<string, unknown>;
        const durationMs = typeof digest.duration_ms === 'number' ? digest.duration_ms : undefined;
        const label = t.name.length > 60 ? t.name.slice(0, 60) + '\u2026' : t.name;
        const startedAt = t.started_at
          ? new Date(t.started_at).toLocaleTimeString()
          : t.created_at
          ? new Date(t.created_at).toLocaleTimeString()
          : '';
        return {
          id: t.task_id,
          label,
          status: _mapTaskStatus(t.status),
          startedAt,
          duration: durationMs,
          error: t.error ?? undefined,
          _start: 0,
        };
      });
  }, [rootTasks]);
}

// Stable empty reference for tree hook.
const _EMPTY_TREE: TaskNode[] = [];

/**
 * Returns the full task tree for a widget, including all children and
 * metadata.  Unlike useWidgetTasks this does **not** filter by task_type
 * nor strip children — it returns the raw TaskNode[] with nested children
 * intact, suitable for AgentTreePanel to walk recursively.
 */
export function useWidgetTaskTree(psId: string, widgetId: string): TaskNode[] {
  const sessionKey = `${psId}__${widgetId}`;
  const rootTasks = useTasksStore((s) => s.sessions[sessionKey] ?? _EMPTY_TREE);
  return rootTasks;
}

// Stable empty reference for agent tree hook.
const _EMPTY_AGENTS: AgentNode[] = [];

/**
 * Returns the Agent tree for a widget, built by the backend from the full
 * TaskNode tree.  The backend walks SUBAGENT_SPAWN tasks recursively to
 * produce a clean  Main Agent → SubAgent → ...  hierarchy that the frontend
 * can render directly without fragile client-side tree walking.
 *
 * Polled at 2 s so the tree stays current while tasks are being created.
 */
export function useAgentTree(psId: string, widgetId: string) {
  const query = useQuery({
    queryKey: ['agent-tree', psId, widgetId],
    queryFn: () => api.getAgentTree(psId, widgetId),
    refetchInterval: 2_000,
    enabled: !!psId && !!widgetId,
  });

  return {
    agents: query.data?.agents ?? _EMPTY_AGENTS,
    ...query,
  };
}
