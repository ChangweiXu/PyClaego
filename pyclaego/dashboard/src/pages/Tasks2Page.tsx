/**
 * Tasks2Page — alternative task browser at /tasks2.
 *
 * Layout (all panes separated by draggable splitters):
 *
 *   ┌──────────────┬─────────────┬──────────────────────────────────────┐
 *   │ PS list  20% │ Widget  15% │  Task Tree                      65%  │
 *   ╞══════════════╧═════════════╧════════════ drag ════════════════════╡
 *   │  Task info / description        50%  │  Metadata & Artifacts 50%  │
 *   └──────────────────────────── drag ───────────────────────────────┘
 */
import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type WidgetSummary, type TaskNode } from '../api';
import { useTasksStore } from '../store/tasks';
import { useUIStore } from '../store/ui';
import { useTasks } from '../queries/tasks';
import { tasksWS } from '../ws/tasksWS';
import { TaskTree } from '../components/TaskTree';
import { useDraggableSplit } from '../hooks/useDraggableSplit';
import { TaskInfoPane, TYPE_ICON, STATUS_COLOR } from '../components/TaskInfoPane';
import { TaskMetaPane } from '../components/TaskMetaPane';
import { findTaskById } from '../utils/tasks';

// ─── PS Column ────────────────────────────────────────────────────────────────

function PSColumn({
  psIds, taskCountByPS, selected, onSelect,
}: {
  psIds: string[];
  taskCountByPS: Record<string, number>;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="tasks2-col tasks2-col--ps">
      <div className="tasks-section-label">Personal Spaces</div>
      {psIds.length === 0 && <div className="tasks-empty">No spaces</div>}
      {psIds.map((id) => (
        <div
          key={id}
          className={`tasks-session-row${selected === id ? ' tasks-session-row--active' : ''}`}
          onClick={() => onSelect(id)}
        >
          <span className="tasks-session-toggle">{selected === id ? '▾' : '▸'}</span>
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: '0.82rem', fontWeight: 600 }}>{id}</span>
          {(taskCountByPS[id] ?? 0) > 0 && <span className="tasks-count-badge">{taskCountByPS[id]}</span>}
        </div>
      ))}
    </div>
  );
}

// ─── Widget Column ────────────────────────────────────────────────────────────

function WidgetColumn({
  psId, sessionKeys, taskCountByWidget, selected, onSelect,
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
  const apiWidgets: WidgetSummary[] = psSummary?.widgets ?? [];
  const apiWidgetIds = new Set(apiWidgets.map((w) => w.widget_id));
  const sessionWidgetIds = sessionKeys
    .filter((k) => psId && k.startsWith(psId + '__'))
    .map((k) => k.slice((psId + '__').length));
  const allWidgetIds = [
    ...apiWidgets.map((w) => w.widget_id),
    ...sessionWidgetIds.filter((id) => !apiWidgetIds.has(id)),
  ];
  const titleOf = (wid: string) => apiWidgets.find((w) => w.widget_id === wid)?.title ?? wid;
  const classOf = (wid: string) => apiWidgets.find((w) => w.widget_id === wid)?.widget_class ?? '';

  if (!psId) {
    return (
      <div className="tasks2-col tasks2-col--widget">
        <div className="tasks-section-label">Widgets</div>
        <div className="tasks-empty">Select a PS</div>
      </div>
    );
  }

  return (
    <div className="tasks2-col tasks2-col--widget">
      <div className="tasks-section-label">Widgets</div>
      {allWidgetIds.length === 0 && <div className="tasks-empty">No widgets</div>}
      {allWidgetIds.map((wid) => {
        const key = `${psId}__${wid}`;
        const count = taskCountByWidget[key] ?? 0;
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
              {classOf(wid) && (
                <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: 1 }}>{classOf(wid)}</div>
              )}
            </div>
            {count > 0 && <span className="tasks-count-badge">{count}</span>}
          </div>
        );
      })}
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function Tasks2Page() {
  const [selectedPS, setSelectedPS] = useState<string | null>(null);
  const [selectedWidgetKey, setSelectedWidgetKey] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(() => tasksWS.connected);

  const sessions = useTasksStore((s) => s.sessions);
  const selectedTaskId = useUIStore((s) => s.selectedTaskId);

  useTasks();
  useEffect(() => tasksWS.onConnectedChange(setWsConnected), []);
  useEffect(() => { setSelectedWidgetKey(null); }, [selectedPS]);

  const { data: psListData } = useQuery({
    queryKey: ['ps-list'],
    queryFn: () => api.listPersonalSpaces(),
    staleTime: 15_000,
  });
  const psIds = psListData?.personal_spaces ?? [];
  const sessionKeys = Object.keys(sessions);

  const apiPsIdSet = new Set(psIds);
  const extraPsIds = [...new Set(sessionKeys.map((k) => k.split('__')[0]))].filter(
    (id) => !apiPsIdSet.has(id),
  );
  const allPsIds = [...psIds, ...extraPsIds];

  const taskCountByWidget: Record<string, number> = {};
  for (const key of sessionKeys) taskCountByWidget[key] = sessions[key]?.length ?? 0;

  const taskCountByPS: Record<string, number> = {};
  for (const key of sessionKeys) {
    const ps = key.split('__')[0];
    taskCountByPS[ps] = (taskCountByPS[ps] ?? 0) + (taskCountByWidget[key] ?? 0);
  }

  const rootTasks = selectedWidgetKey ? (sessions[selectedWidgetKey] ?? []) : [];
  const selectedTaskNode = selectedTaskId ? findTaskById(rootTasks, selectedTaskId) : null;

  // ── Splitters ──────────────────────────────────────────────────────────────
  const bodyRef   = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Vertical splitter divides top row from bottom row (initial: 50%)
  const [hRatio, hSplitterProps] = useDraggableSplit(
    'h', bodyRef as RefObject<HTMLElement>, 0.5,
  );
  // Horizontal splitter divides bottom-left from bottom-right (initial: 50%)
  const [vRatio, vSplitterProps] = useDraggableSplit(
    'v', bottomRef as RefObject<HTMLElement>, 0.5,
  );

  return (
    <div className="tasks2-page">
      <div className="tasks-page-header">
        <h3 style={{ flex: 1 }}>Tasks (v2)</h3>
        <span
          className={`tasks-ws-dot${wsConnected ? ' tasks-ws-dot--on' : ''}`}
          title={wsConnected ? 'Live (WebSocket)' : 'Polling'}
        />
        <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
          {wsConnected ? 'Live' : 'Polling'}
        </span>
      </div>

      <div className="tasks2-body" ref={bodyRef}>
        {/* ── Top row: PS | Widget | Tree ─────────────────────────────────── */}
        <div className="tasks2-top" style={{ height: `${hRatio * 100}%` }}>
          <PSColumn
            psIds={allPsIds}
            taskCountByPS={taskCountByPS}
            selected={selectedPS}
            onSelect={setSelectedPS}
          />
          <WidgetColumn
            psId={selectedPS}
            sessionKeys={sessionKeys}
            taskCountByWidget={taskCountByWidget}
            selected={selectedWidgetKey}
            onSelect={setSelectedWidgetKey}
          />
          <div className="tasks2-col tasks2-col--tree">
            {!selectedWidgetKey ? (
              <div className="tasks-empty" style={{ padding: '1rem' }}>Select a widget</div>
            ) : rootTasks.length === 0 ? (
              <div className="tasks-empty" style={{ padding: '1rem' }}>No tasks</div>
            ) : (
              <TaskTree nodes={rootTasks} />
            )}
          </div>
        </div>

        {/* ── Horizontal drag handle ───────────────────────────────────────── */}
        <div className="tasks2-hsplitter" {...hSplitterProps} />

        {/* ── Bottom row: Info | vsplitter | Meta+Artifacts ───────────────── */}
        <div className="tasks2-bottom" ref={bottomRef}>
          <div className="tasks2-bottom-pane" style={{ width: `${vRatio * 100}%` }}>
            <TaskInfoPane task={selectedTaskNode} />
          </div>
          <div className="tasks2-vsplitter" {...vSplitterProps} />
          <div className="tasks2-bottom-pane" style={{ flex: 1, width: 0 }}>
            <TaskMetaPane task={selectedTaskNode} />
          </div>
        </div>
      </div>
    </div>
  );
}
