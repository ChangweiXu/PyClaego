/**
 * tasksWS — singleton WebSocket client for /ws/tasks.
 *
 * Connects once at app boot (tasksWS.start()), auto-reconnects on drop.
 *
 * Protocol (chunked):
 *   snapshot_chunk  → buffer per-session flat task dicts
 *   snapshot_done   → commit buffer to store via setFlatSnapshot
 *   task_update     → patch single task via patchSession (O(1))
 *   initial_snapshot → legacy fallback (kept for compatibility)
 *
 * Usage:
 *   tasksWS.start()                  — call once at app boot
 *   tasksWS.onConnectedChange(cb)    — subscribe to connect/disconnect
 *   tasksWS.connected                — current state
 *   tasksWS.shutdown()               — hot-reload / unmount only
 */

import { useTasksStore } from '../store/tasks';
import type { RawTaskSnapshot } from '../store/tasks';
import { buildTreeFromFlat } from '../store/tasks';
import type { TaskNode } from '../api';

const WS_PATH = '/ws/tasks';
const RECONNECT_DELAY = 3000;

type ConnectedListener = (connected: boolean) => void;

interface TasksWSMessage {
  type: string;
  [key: string]: unknown;
}

class TasksWSClient {
  private ws: WebSocket | null = null;
  private intentionallyClosed = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private connListeners = new Set<ConnectedListener>();

  /** Buffer accumulating snapshot_chunk messages until snapshot_done. */
  private _snapshotBuffer: Record<string, Record<string, RawTaskSnapshot>> = {};

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  start(): void {
    if (this.ws !== null) return;
    this.intentionallyClosed = false;
    this._connect();
  }

  onConnectedChange(cb: ConnectedListener): () => void {
    this.connListeners.add(cb);
    return () => this.connListeners.delete(cb);
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  shutdown(): void {
    this.intentionallyClosed = true;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    try { this.ws?.close(); } catch { /* ignore */ }
    this.ws = null;
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private _connect(): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}${WS_PATH}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._snapshotBuffer = {};
      this.connListeners.forEach((l) => l(true));
    };

    this.ws.onmessage = (ev) => {
      let msg: TasksWSMessage;
      try {
        msg = JSON.parse(ev.data as string) as TasksWSMessage;
      } catch {
        return;
      }
      this._handleMessage(msg);
    };

    this.ws.onclose = () => {
      this.connListeners.forEach((l) => l(false));
      if (!this.intentionallyClosed) {
        this.retryTimer = setTimeout(() => this._connect(), RECONNECT_DELAY);
      }
    };

    this.ws.onerror = () => {
      // onclose fires after onerror; reconnect handled there
      try { this.ws?.close(); } catch { /* ignore */ }
    };
  }

  private _handleMessage(msg: TasksWSMessage): void {
    const store = useTasksStore.getState();

    switch (msg.type) {
      case 'snapshot_chunk': {
        const sid = msg['session_id'] as string;
        const tasks = msg['tasks'] as Record<string, RawTaskSnapshot>;
        if (sid && tasks) {
          this._snapshotBuffer[sid] = tasks;
        }
        break;
      }

      case 'snapshot_done': {
        store.setFlatSnapshot(this._snapshotBuffer);
        this._snapshotBuffer = {};
        break;
      }

      case 'task_update': {
        const sid = msg['session_id'] as string;
        const taskId = msg['task_id'] as string;
        const snapshot = msg['task_snapshot'] as RawTaskSnapshot | null;
        if (sid && taskId && snapshot) {
          store.patchSession(sid, { [taskId]: snapshot });
        }
        break;
      }

      case 'initial_snapshot': {
        // Legacy fallback: server sends full tree in task_tree field
        const raw = msg['task_tree'] as Record<string, unknown> | undefined;
        if (!raw) break;
        let flat: Record<string, TaskNode[]>;
        if (raw['sessions'] && typeof raw['sessions'] === 'object' && !Array.isArray(raw['sessions'])) {
          const inner = raw['sessions'] as Record<string, unknown>;
          flat = {};
          for (const [sid, val] of Object.entries(inner)) {
            flat[sid] = Array.isArray(val)
              ? (val as TaskNode[])
              : ((val as { root_tasks?: TaskNode[] }).root_tasks ?? []);
          }
        } else {
          flat = raw as Record<string, TaskNode[]>;
        }
        store.setSnapshot(flat);
        break;
      }

      default:
        break;
    }
  }
}

export const tasksWS = new TasksWSClient();
