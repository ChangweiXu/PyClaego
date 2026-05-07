/**
 * TasksPage — full-page three-column task browser at /tasks.
 *
 * Layout:
 *   ┌──────────────┬─────────────────────┬────────────────────────────────┐
 *   │  PS list     │  Widget list        │  Task Tree          (flex: 1)  │
 *   │  20%         │  30%                ├────────────────────────────────┤
 *   │              │                     │  Task Detail        (flex: 1)  │
 *   └──────────────┴─────────────────────┴────────────────────────────────┘
 *
 * Data:
 *   - PS column:     api.listPersonalSpaces() + task counts from useTasksStore
 *   - Widget column: api.getPS(selectedPS) widgets + matching session_ids in store
 *   - Tree / Detail: useTasksStore.sessions[selectedWidgetKey]
 *   - selectedTaskId: shared with TasksDrawer via useUIStore
 */
import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type WidgetSummary } from '../api';
import { useTasksStore } from '../store/tasks';
import { useUIStore } from '../store/ui';
import { useTasks } from '../queries/tasks';
import { tasksWS } from '../ws/tasksWS';
import { TaskTree } from '../components/TaskTree';
import TaskDetailPane from '../components/TaskDetailPane';
import { findTaskById } from '../utils/tasks';

// ─── PS Column ───────────────────────────────────────────────────────────────

function PSColumn({
  psIds,
  taskCountByPS,
  selected,
  onSelect,
}: {
  psIds: string[];
  taskCountByPS: Record<string, number>;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="tasks-page-col tasks-page-col--ps">
      <div className="tasks-section-label">Personal Spaces</div>
      {psIds.length === 0 && <div className="tasks-empty">No spaces</div>}
      {psIds.map((id) => {
        const count = taskCountByPS[id] ?? 0;
        return (
          <div
            key={id}
            className={`tasks-session-row${selected === id ? ' tasks-session-row--active' : ''}`}
            onClick={() => onSelect(id)}
          >
            <span className="tasks-session-toggle">{selected === id ? '▾' : '▸'}</span>
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: '0.82rem', fontWeight: 600 }}>{id}</span>
            {count > 0 && <span className="tasks-count-badge">{count}</span>}
          </div>
        );
      })}
    </div>
  );
}

// ─── Widget Column ───────────────────────────────────────────────────────────

function WidgetColumn({
  psId,
  sessionKeys,
  taskCountByWidget,
  selected,
  onSelect,
}: {
  psId: string | null;
  sessionKeys: string[];
  taskCountByWidget: Record<string, number>;
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const { data: psSummary } = useQuery({
    queryKey: ['ps', psId],
    queryFn: () => api.getPS(psId!),
    enabled: !!psId,
    staleTime: 30_000,
  });

  // Merge: widgets from API + widgets only known from sessions
  const apiWidgets: WidgetSummary[] = psSummary?.widgets ?? [];
  const apiWidgetIds = new Set(apiWidgets.map((w) => w.widget_id));

  // session keys are like "psId__widgetId", extract the widget part
  const sessionWidgetIds = sessionKeys
    .filter((k) => psId && k.startsWith(psId + '__'))
    .map((k) => k.slice((psId + '__').length));

  // Union: API widgets first, then any session-only widgetIds
  const allWidgetIds: string[] = [
    ...apiWidgets.map((w) => w.widget_id),
    ...sessionWidgetIds.filter((id) => !apiWidgetIds.has(id)),
  ];

  const titleOf = (wid: string) => apiWidgets.find((w) => w.widget_id === wid)?.title ?? wid;
  const classOf = (wid: string) => apiWidgets.find((w) => w.widget_id === wid)?.widget_class ?? '';

  if (!psId) {
    return (
      <div className="tasks-page-col tasks-page-col--widget">
        <div className="tasks-section-label">Widgets</div>
        <div className="tasks-empty">Select a PS</div>
      </div>
    );
  }

  return (
    <div className="tasks-page-col tasks-page-col--widget">
      <div className="tasks-section-label">Widgets</div>
      {allWidgetIds.length === 0 && <div className="tasks-empty">No widgets</div>}
      {allWidgetIds.map((wid) => {
        const key = `${psId}__${wid}`;
        const count = taskCountByWidget[key] ?? 0;
        const cls = classOf(wid);
        return (
          <div
            key={wid}
            className={`tasks-session-row${selected === key ? ' tasks-session-row--active' : ''}`}
            onClick={() => onSelect(key)}
          >
            <span className="tasks-session-toggle">{selected === key ? '▾' : '▸'}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.82rem', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {titleOf(wid)}
              </div>
              {cls && (
                <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: 1 }}>{cls}</div>
              )}
            </div>
            {count > 0 && <span className="tasks-count-badge">{count}</span>}
          </div>
        );
      })}
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function TasksPage() {
  const [selectedPS, setSelectedPS] = useState<string | null>(null);
  const [selectedWidgetKey, setSelectedWidgetKey] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(() => tasksWS.connected);

  const sessions = useTasksStore((s) => s.sessions);
  const selectedTaskId = useUIStore((s) => s.selectedTaskId);

  // Trigger REST fallback polling (WS writes happen independently)
  useTasks();

  // Track WS status
  useEffect(() => tasksWS.onConnectedChange(setWsConnected), []);

  // Reset widget selection when PS changes
  useEffect(() => { setSelectedWidgetKey(null); }, [selectedPS]);

  // PS list
  const { data: psListData } = useQuery({
    queryKey: ['ps-list'],
    queryFn: () => api.listPersonalSpaces(),
    staleTime: 15_000,
  });
  const psIds = psListData?.personal_spaces ?? [];

  // Derive task counts per PS and per widget from the live store
  const sessionKeys = Object.keys(sessions);

  // Include any PS IDs present only in the task store (e.g. feishu PSs excluded from the REST list)
  const apiPsIdSet = new Set(psIds);
  const extraPsIds = [...new Set(sessionKeys.map((k) => k.split('__')[0]))].filter(
    (id) => !apiPsIdSet.has(id),
  );
  const allPsIds = [...psIds, ...extraPsIds];

  const taskCountByWidget: Record<string, number> = {};
  for (const key of sessionKeys) {
    taskCountByWidget[key] = sessions[key]?.length ?? 0;
  }

  const taskCountByPS: Record<string, number> = {};
  for (const key of sessionKeys) {
    const parts = key.split('__');
    const ps = parts[0];
    taskCountByPS[ps] = (taskCountByPS[ps] ?? 0) + (taskCountByWidget[key] ?? 0);
  }

  // Root tasks for the selected widget
  const rootTasks = selectedWidgetKey ? (sessions[selectedWidgetKey] ?? []) : [];

  // Find selected task node across all root tasks
  const allNodes = rootTasks;
  const selectedTaskNode = selectedTaskId ? findTaskById(allNodes, selectedTaskId) : null;

  return (
    <div className="tasks-page">
      {/* Header */}
      <div className="tasks-page-header">
        <h3 style={{ flex: 1 }}>Tasks</h3>
        <span
          className={`tasks-ws-dot${wsConnected ? ' tasks-ws-dot--on' : ''}`}
          title={wsConnected ? 'Live (WebSocket)' : 'Polling (no WS)'}
        />
        <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
          {wsConnected ? 'Live' : 'Polling'}
        </span>
      </div>

      {/* Three-column body */}
      <div className="tasks-page-body">
        {/* Col 1 — PS list (20%) */}
        <PSColumn
          psIds={allPsIds}
          taskCountByPS={taskCountByPS}
          selected={selectedPS}
          onSelect={setSelectedPS}
        />

        {/* Col 2 — Widget list (30%) */}
        <WidgetColumn
          psId={selectedPS}
          sessionKeys={sessionKeys}
          taskCountByWidget={taskCountByWidget}
          selected={selectedWidgetKey}
          onSelect={setSelectedWidgetKey}
        />

        {/* Col 3 — Task tree (top) + Detail (bottom) (50%) */}
        <div className="tasks-page-col tasks-page-col--right">
          {/* Tree pane */}
          <div className="tasks-page-pane tasks-page-pane--tree">
            {!selectedWidgetKey ? (
              <div className="tasks-empty" style={{ padding: '1rem' }}>Select a widget to view its task tree</div>
            ) : rootTasks.length === 0 ? (
              <div className="tasks-empty" style={{ padding: '1rem' }}>No tasks for this widget</div>
            ) : (
              <TaskTree nodes={rootTasks} />
            )}
          </div>

          {/* Detail pane */}
          <div className="tasks-page-pane tasks-page-pane--detail">
            <TaskDetailPane task={selectedTaskNode} />
          </div>
        </div>
      </div>
    </div>
  );
}
