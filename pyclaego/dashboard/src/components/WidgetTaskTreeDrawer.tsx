/**
 * WidgetTaskTreeDrawer — left-side slide-in drawer showing the task tree
 * for a specific chat widget.
 *
 * Layout (replicates Tasks2Page bottom section):
 *
 *   ┌──────────────────────────────────────────────────────┐
 *   │  Header (psId · widgetId · WS dot · close)           │
 *   ├──────────────────────────────────────────────────────┤
 *   │  Task Tree                                           │
 *   ╞══════════════════════════════ drag ══════════════════╡
 *   │  Task Info / description   50%  │  Meta+Artifacts 50%│
 *   └──────────────────────────────────────────────────────┘
 *
 * Data sourced from useTasksStore (live via tasksWS + REST fallback).
 */

import { useEffect, useRef, useState, useMemo } from 'react';
import { useTasksStore } from '../store/tasks';
import { useUIStore } from '../store/ui';
import { tasksWS } from '../ws/tasksWS';
import { TaskTree } from './TaskTree';
import { TaskInfoPane } from './TaskInfoPane';
import { TaskMetaPane } from './TaskMetaPane';
import { useDraggableSplit } from '../hooks/useDraggableSplit';
import { findTaskById } from '../utils/tasks';
import type { TaskNode } from '../api';

export interface WidgetTaskTreeDrawerProps {
  open: boolean;
  psId: string;
  widgetId: string;
  onClose: () => void;
}

export function WidgetTaskTreeDrawer({ open, psId, widgetId, onClose }: WidgetTaskTreeDrawerProps) {
  const [wsConnected, setWsConnected] = useState(() => tasksWS.connected);

  // WS connection state
  useEffect(() => tasksWS.onConnectedChange(setWsConnected), []);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    if (open) {
      document.addEventListener('keydown', handler);
      return () => document.removeEventListener('keydown', handler);
    }
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div className="drawer-backdrop" onClick={onClose} />

      {/* Drawer panel */}
      <div className="wtree-drawer" role="dialog" aria-label="Task Tree">
        <WidgetTaskTreeDrawerInner
          psId={psId}
          widgetId={widgetId}
          wsConnected={wsConnected}
          onClose={onClose}
        />
      </div>
    </>
  );
}

/** Inner component — only mounted when drawer is open, so hooks are safe. */
function WidgetTaskTreeDrawerInner({
  psId, widgetId, wsConnected, onClose,
}: {
  psId: string;
  widgetId: string;
  wsConnected: boolean;
  onClose: () => void;
}) {
  const sessionKey = `${psId}__${widgetId}`;
  const sessions = useTasksStore((s) => s.sessions);
  const rootTasks: TaskNode[] = useMemo(() => sessions[sessionKey] ?? [], [sessions, sessionKey]);

  const selectedTaskId = useUIStore((s) => s.selectedTaskId);
  const selectedTaskNode = selectedTaskId ? findTaskById(rootTasks, selectedTaskId) : null;

  // ── Splitters ──────────────────────────────────────────────────────────
  const bodyRef   = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const [hRatio, hSplitterProps] = useDraggableSplit('h', bodyRef, 0.5);
  const [vRatio, vSplitterProps] = useDraggableSplit('v', bottomRef, 0.5);

  return (
    <>
      {/* Header */}
      <div className="drawer-header">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h3>Task Tree</h3>
          <div className="meta">{psId} · {widgetId}</div>
        </div>
        <span
          className={`tasks-ws-dot${wsConnected ? ' tasks-ws-dot--on' : ''}`}
          title={wsConnected ? 'Live (WebSocket)' : 'Polling (no WS)'}
        />
        <button className="drawer-close" onClick={onClose} aria-label="Close">✕</button>
      </div>

      {/* Body */}
      <div className="wtree-body" ref={bodyRef}>
        {/* ── Top: Task Tree ──────────────────────────────────────────────── */}
        <div className="wtree-top" style={{ height: `${hRatio * 100}%` }}>
          {rootTasks.length === 0 ? (
            <div className="tasks-empty" style={{ padding: '1rem' }}>No tasks for this widget</div>
          ) : (
            <TaskTree nodes={rootTasks} />
          )}
        </div>

        {/* ── Horizontal drag handle ───────────────────────────────────────── */}
        <div className="wtree-hsplitter" {...hSplitterProps} />

        {/* ── Bottom: Info | Meta+Artifacts ───────────────────────────────── */}
        <div className="wtree-bottom" ref={bottomRef}>
          <div className="wtree-bottom-pane" style={{ width: `${vRatio * 100}%` }}>
            <TaskInfoPane task={selectedTaskNode} />
          </div>
          <div className="wtree-vsplitter" {...vSplitterProps} />
          <div className="wtree-bottom-pane" style={{ flex: 1, width: 0 }}>
            <TaskMetaPane task={selectedTaskNode} />
          </div>
        </div>
      </div>
    </>
  );
}
