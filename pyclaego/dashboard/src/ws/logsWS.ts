/**
 * logsWS — singleton WebSocket client for /ws/logs.
 *
 * Connects on first start(), auto-reconnects on drop.
 *
 * Protocol (server → client):
 *   { type: "tree_update", tree: LogTreeNode[] }  — full tree rebuild
 *   { type: "ping" }                               — keepalive (ignored)
 *
 * Usage:
 *   logsWS.start()                  — call once on page mount
 *   logsWS.stop()                   — call on page unmount
 *   logsWS.onTree(cb)               — subscribe to tree updates
 *   logsWS.onConnectedChange(cb)    — subscribe to connect/disconnect events
 */

import type { LogTreeNode } from '../pages/LogsPage/logsApi';

const WS_PATH = '/ws/logs';
const RECONNECT_DELAY = 3000;

type TreeListener = (tree: LogTreeNode[]) => void;
type ConnectedListener = (connected: boolean) => void;

interface LogsWSMessage {
  type: string;
  tree?: LogTreeNode[];
}

class LogsWSClient {
  private ws: WebSocket | null = null;
  private intentionallyClosed = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private treeListeners = new Set<TreeListener>();
  private connListeners = new Set<ConnectedListener>();

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  start(): void {
    if (this.ws !== null) return;
    this.intentionallyClosed = false;
    this._connect();
  }

  stop(): void {
    this.intentionallyClosed = true;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    try { this.ws?.close(); } catch { /* ignore */ }
    this.ws = null;
  }

  /** Subscribe to tree update messages. Returns an unsubscribe function. */
  onTree(cb: TreeListener): () => void {
    this.treeListeners.add(cb);
    return () => this.treeListeners.delete(cb);
  }

  /** Subscribe to connect/disconnect events. Returns an unsubscribe function. */
  onConnectedChange(cb: ConnectedListener): () => void {
    this.connListeners.add(cb);
    return () => this.connListeners.delete(cb);
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private _connect(): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}${WS_PATH}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._notifyConn(true);
    };

    this.ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as LogsWSMessage;
        if (msg.type === 'tree_update' && msg.tree) {
          for (const cb of this.treeListeners) cb(msg.tree);
        }
        // pings are silently ignored
      } catch {
        // ignore malformed messages
      }
    };

    this.ws.onerror = () => { /* onclose will follow */ };

    this.ws.onclose = () => {
      this.ws = null;
      this._notifyConn(false);
      if (!this.intentionallyClosed) {
        this.retryTimer = setTimeout(() => {
          this.retryTimer = null;
          this._connect();
        }, RECONNECT_DELAY);
      }
    };
  }

  private _notifyConn(state: boolean): void {
    for (const cb of this.connListeners) cb(state);
  }
}

export const logsWS = new LogsWSClient();
