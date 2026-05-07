/**
 * TasksDrawer — left-side slide-in panel for the global task tree.
 *
 * Layout:
 *   ┌────────────────────────────────────────────┐
 *   │  Header                                    │
 *   ├──────────────────┬─────────────────────────┤
 *   │  Nav (sessions / │  Detail pane            │
 *   │  turns / tree)   │  (selected task node)   │
 *   └──────────────────┴─────────────────────────┘
 *
 * Data flows from:
 *   - tasksWS singleton  → writes to useTasksStore (primary, live)
 *   - useTasks() hook    → REST poll at 5s (fallback)
 */
import { useEffect, useState } from 'react';
import { useTasksStore } from '../store/tasks';
import { useTasks } from '../queries/tasks';
import { tasksWS } from '../ws/tasksWS';
import { TaskTree } from './TaskTree';
import TaskDetailPane from './TaskDetailPane';
import { useUIStore } from '../store/ui';
import type { TaskNode } from '../api';
import { sessionChips, turnLabel, findTaskById } from '../utils/tasks';

interface Props {
  onClose: () => void;
}


export default function TasksDrawer({ onClose }: Props) {
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(() => tasksWS.connected);

  const selectedTaskId = useUIStore((s) => s.selectedTaskId);

  // Track WS connection state
  useEffect(() => {
    return tasksWS.onConnectedChange(setWsConnected);
  }, []);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  // Trigger REST polling (WS store writes happen independently)
  useTasks();

  const sessions = useTasksStore((s) => s.sessions);
  const sessionIds = Object.keys(sessions).sort();

  // Root tasks = "turns" for selected session, newest first
  const turns: TaskNode[] = selectedSession
    ? [...(sessions[selectedSession] ?? [])].reverse()
    : [];

  const selectedTurn = turns.find((t) => t.task_id === selectedTurnId) ?? null;

  // Resolve the selected task node from the whole visible tree
  const allVisibleNodes: TaskNode[] = selectedTurn ? [selectedTurn] : turns;
  const selectedTaskNode: TaskNode | null = selectedTaskId
    ? findTaskById(allVisibleNodes.length ? allVisibleNodes : Object.values(sessions).flat(), selectedTaskId)
    : null;

  // Clear turn selection when session changes
  useEffect(() => { setSelectedTurnId(null); }, [selectedSession]);

  function toggleSession(sid: string) {
    setSelectedSession((prev) => (prev === sid ? null : sid));
  }

  function toggleTurn(tid: string) {
    setSelectedTurnId((prev) => (prev === tid ? null : tid));
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="tasks-drawer" role="dialog" aria-label="Tasks">
        {/* Header */}
        <div className="drawer-header">
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3>Tasks</h3>
          </div>
          <span
            className={`tasks-ws-dot${wsConnected ? ' tasks-ws-dot--on' : ''}`}
            title={wsConnected ? 'Live (WebSocket)' : 'Polling (no WS)'}
          />
          <button className="drawer-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        {/* Split body */}
        <div className="tasks-drawer-split">
          {/* ── Nav pane ────────────────────────────────────────── */}
          <div className="tasks-drawer-nav">
            {/* Section 1: Sessions */}
            <div className="tasks-section-label">
              Sessions
              <span className="tasks-count-badge">{sessionIds.length}</span>
            </div>

            {sessionIds.length === 0 && (
              <div className="tasks-empty">No active sessions</div>
            )}

            {sessionIds.map((sid) => {
              const isActive = selectedSession === sid;
              const rootCount = sessions[sid]?.length ?? 0;
              return (
                <div key={sid}>
                  <div
                    className={`tasks-session-row${isActive ? ' tasks-session-row--active' : ''}`}
                    onClick={() => toggleSession(sid)}
                  >
                    <span className="tasks-session-toggle">{isActive ? '▾' : '▸'}</span>
                    <div className="tasks-session-chips">
                      {sessionChips(sid).map((chip, i) => (
                        <span key={i} className="tasks-chip">{chip}</span>
                      ))}
                    </div>
                    <span className="tasks-count-badge">{rootCount}</span>
                  </div>

                  {/* Section 2: Turns (inline under session) */}
                  {isActive && (
                    <div className="tasks-turns-list">
                      {turns.length === 0 && (
                        <div className="tasks-empty tasks-empty--indent">No turns yet</div>
                      )}
                      {turns.map((turn) => {
                        const isTurnActive = selectedTurnId === turn.task_id;
                        return (
                          <div key={turn.task_id}>
                            <div
                              className={`tasks-turn-row${isTurnActive ? ' tasks-turn-row--active' : ''}`}
                              onClick={() => toggleTurn(turn.task_id)}
                            >
                              <span className="tasks-turn-toggle">{isTurnActive ? '▾' : '▸'}</span>
                              <span className="tasks-turn-name">{turnLabel(turn)}</span>
                              <span className={`tasks-status-badge tasks-status-badge--${turn.status}`}>
                                {turn.status}
                              </span>
                            </div>

                            {/* Section 3: Task Tree */}
                            {isTurnActive && (
                              <div className="tasks-tree-wrap">
                                <TaskTree nodes={[turn]} />
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* ── Detail pane ─────────────────────────────────────── */}
          <div className="tasks-drawer-detail">
            <TaskDetailPane task={selectedTaskNode} />
          </div>
        </div>
      </div>
    </>
  );
}

