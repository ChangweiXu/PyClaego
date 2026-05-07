/**
 * notesRpc.ts — JSON-RPC 2.0 client over WebSocket for the note system.
 *
 * Replaces notesApi.ts (REST + separate events WS) with a single connection
 * that carries both RPC calls and server-push VaultEvent notifications.
 *
 * Usage:
 *   const client = new NoteRpcClient(psId, widgetId);
 *   client.connect();                        // opens WS + auto-calls vault.open
 *   client.onVaultEvent((ev) => { ... });    // subscribe to server notifications
 *   const tree = await client.listFiles();   // typed RPC call
 *   client.disconnect();                     // graceful close
 */

// ── Domain types (previously in notesApi.ts) ─────────────────────────────

export interface DocMeta {
  doc_id: string;
  rel_path: string;
  title: string | null;
  created_at: number;
  modified_at: number;
  stub?: boolean;
}

export interface FileTreeNode {
  type: 'file' | 'dir';
  name: string;
  rel_path: string;
  children?: FileTreeNode[];
}

export interface SearchResult {
  doc_id: string;
  rel_path: string;
  title: string | null;
  snippet: string;
}

export interface TagInfo {
  tag_id: number;
  tag_name: string;
  doc_count: number;
}

export interface GraphData {
  nodes: { id: string; label: string; rel_path: string; stub?: boolean }[];
  edges: { source: string; target: string; display_text?: string }[];
}

export interface BacklinkEntry {
  doc_id: string;
  rel_path: string;
  title: string | null;
  block_anchor: string | null;
  snippet: string;
}

export interface ResolvedLink {
  rel_path: string;
  doc_id: string | null;
  block_id: string;
  exists: boolean;
}

export interface DocIdInfo {
  doc_id: string;
  rel_path: string;
  title: string | null;
}

export interface AutocompleteCandidate {
  value: string;
  label: string;
}

export interface AutocompleteResult {
  prefix: string;
  kind: 'tag' | 'link';
  candidates: AutocompleteCandidate[];
}

// ── Protocol types ────────────────────────────────────────────────────────

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: number;
  method: string;
  params: Record<string, unknown>;
}

export interface JsonRpcSuccessResponse<T = unknown> {
  jsonrpc: '2.0';
  id: number;
  result: T;
}

export interface JsonRpcErrorResponse {
  jsonrpc: '2.0';
  id: number;
  error: { code: number; message: string; data?: unknown };
}

export type JsonRpcResponse<T = unknown> = JsonRpcSuccessResponse<T> | JsonRpcErrorResponse;

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: unknown;
}

// ── VaultEvent (matches backend VaultEvent dataclass) ────────────────────

export type VaultEventType = 'created' | 'modified' | 'deleted' | 'renamed' | 'index_ready';

export interface VaultEvent {
  vault_id: string;
  type: VaultEventType;
  rel_path: string;
  doc_id?: string;
  title?: string;
  modified_at?: number;
  old_path?: string;
}

// ── Internal types ────────────────────────────────────────────────────────

type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
};

type NotificationHandler = (params: unknown) => void;

// ── Helpers ───────────────────────────────────────────────────────────────

function rpcWsUrl(psId: string, widgetId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  return `${proto}//${host}/ws/v2/notes/${encodeURIComponent(psId)}/${encodeURIComponent(widgetId)}/rpc`;
}

// ── NoteRpcClient ─────────────────────────────────────────────────────────

export class NoteRpcClient {
  private ws: WebSocket | null = null;
  private pending = new Map<number, PendingRequest>();
  private nextId = 1;
  private handlers = new Map<string, NotificationHandler[]>();
  private vaultId: string | null = null;
  private docRoot: string | null = null;
  private closed = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly psId: string,
    private readonly widgetId: string,
  ) {}

  /**
   * Open the WebSocket and automatically call vault.open once connected.
   * @param docRoot Optional custom doc_root; if omitted, the server uses the
   *   widget's configured root.
   */
  connect(docRoot?: string): void {
    this.docRoot = docRoot ?? null;
    this.closed = false;
    this._connect();
  }

  /** Close the connection permanently (no reconnect). */
  disconnect(): void {
    this.closed = true;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.ws?.close();
  }

  /** True if the vault is open and ready for calls. */
  get ready(): boolean {
    return this.vaultId !== null && this.ws?.readyState === WebSocket.OPEN;
  }

  // ── Notification subscription ─────────────────────────────────────────

  /**
   * Register a handler for a JSON-RPC notification method.
   * Returns an unsubscribe function.
   */
  onNotification(method: string, handler: NotificationHandler): () => void {
    const list = this.handlers.get(method) ?? [];
    list.push(handler);
    this.handlers.set(method, list);
    return () => {
      const current = this.handlers.get(method) ?? [];
      this.handlers.set(method, current.filter((h) => h !== handler));
    };
  }

  /** Convenience wrapper for vault.event notifications. */
  onVaultEvent(handler: (event: VaultEvent) => void): () => void {
    return this.onNotification('vault.event', (params) =>
      handler(params as VaultEvent),
    );
  }

  // ── Typed API calls (drop-in for notesApi.*) ──────────────────────────

  listFiles(): Promise<{ tree: FileTreeNode[] }> {
    return this._call('notes.tree', {});
  }

  readFile(relPath: string): Promise<{ rel_path: string; content: Record<string, unknown>; meta: DocMeta | null }> {
    return this._call('notes.read', { rel_path: relPath });
  }

  writeFile(relPath: string, content: Record<string, unknown>): Promise<DocMeta> {
    return this._call('notes.write', { rel_path: relPath, content });
  }

  createFile(relPath: string, content: Record<string, unknown> | string | null = null): Promise<DocMeta> {
    return this._call('notes.create', { rel_path: relPath, content: content ?? {} });
  }

  deleteFile(relPath: string): Promise<{ ok: boolean }> {
    return this._call('notes.delete', { rel_path: relPath });
  }

  renameFile(relPath: string, newPath: string): Promise<DocMeta> {
    return this._call('notes.rename', { rel_path: relPath, new_path: newPath });
  }

  search(q: string, limit = 20): Promise<{ query: string; results: SearchResult[] }> {
    return this._call('notes.search', { query: q, limit });
  }

  graph(): Promise<GraphData> {
    return this._call('notes.graph', {});
  }

  listTags(): Promise<{ tags: TagInfo[] }> {
    return this._call('notes.tags', {});
  }

  docsByTag(tagId: number): Promise<{ tag_id: number; docs: DocMeta[] }> {
    return this._call('notes.docs_by_tag', { tag_id: tagId });
  }

  getBacklinks(relPath: string): Promise<{ rel_path: string; backlinks: BacklinkEntry[] }> {
    return this._call('notes.backlinks', { rel_path: relPath });
  }

  resolveLink(fromPath: string, target: string, anchor = ''): Promise<ResolvedLink> {
    return this._call('notes.resolve_link', { from_path: fromPath, target, anchor });
  }

  resolveDocId(docId: string): Promise<DocIdInfo> {
    return this._call('notes.resolve_doc_id', { doc_id: docId });
  }

  autocomplete(prefix: string, kind: 'tag' | 'link'): Promise<AutocompleteResult> {
    return this._call('notes.autocomplete', { prefix, kind });
  }

  ping(): Promise<{ pong: boolean }> {
    return this._call('system.ping', {});
  }

  /**
   * Creates a comment note in `_comments/` linking back to sourceRelPath#blockId.
   * Returns the created comment's rel_path.
   */
  createComment(sourceRelPath: string, blockId: string, text: string): Promise<string> {
    const ts = Date.now();
    const rand = Math.random().toString(36).slice(2, 7);
    const relPath = `_comments/${ts}-${rand}.bdx`;
    const linkTarget = `../${sourceRelPath}`;
    const safeText = text.replace(/]]>/g, ']]]]><![CDATA[>');
    const xml = [
      `<?xml version="1.0" encoding="utf-8"?>`,
      `<bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">`,
      `  <bdx:body>`,
      `    <bdx:paragraph id="b_c001">`,
      `      <bdx:content><![CDATA[${safeText}]]></bdx:content>`,
      `    </bdx:paragraph>`,
      `    <bdx:paragraph id="b_c002">`,
      `      <bdx:link target="${linkTarget}" anchor="${blockId}" display="source"/>`,
      `    </bdx:paragraph>`,
      `  </bdx:body>`,
      `</bdx:doc>`,
    ].join('\n');
    return this.createFile(relPath, xml).then(() => relPath);
  }

  // ── Internal connection management ────────────────────────────────────

  private _connect(): void {
    if (this.closed) return;
    const url = rpcWsUrl(this.psId, this.widgetId);
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this._openVault().catch((e) => {
        console.error('[NoteRpcClient] vault.open failed', e);
      });
    };

    ws.onmessage = (e: MessageEvent<string>) => {
      try {
        const msg = JSON.parse(e.data) as Record<string, unknown>;
        this._handleMessage(msg);
      } catch {
        /* malformed frame — ignore */
      }
    };

    ws.onclose = () => {
      this.vaultId = null;
      // Reject all pending requests so callers don't hang
      this.pending.forEach(({ reject }) => reject(new Error('WebSocket closed')));
      this.pending.clear();
      if (!this.closed) {
        this.retryTimer = setTimeout(() => this._connect(), 3000);
      }
    };
  }

  private async _openVault(): Promise<void> {
    const params: Record<string, unknown> = {};
    if (this.docRoot) params['doc_root'] = this.docRoot;
    const result = await this._rawCall<{ vault_id: string }>('vault.open', params);
    this.vaultId = result.vault_id;
    // Notify listeners that the vault is ready (fires on every connect / reconnect)
    const handlers = this.handlers.get('vault.opened') ?? [];
    for (const h of handlers) h({ vault_id: this.vaultId });
  }

  private _handleMessage(msg: Record<string, unknown>): void {
    if ('id' in msg && msg['id'] !== undefined && msg['id'] !== null) {
      // Response (success or error)
      const id = msg['id'] as number;
      const pending = this.pending.get(id);
      if (!pending) return;
      this.pending.delete(id);

      if ('error' in msg) {
        const err = msg['error'] as { code: number; message: string };
        pending.reject(new Error(`[${err.code}] ${err.message}`));
      } else {
        pending.resolve(msg['result']);
      }
    } else if ('method' in msg) {
      // Notification
      const method = msg['method'] as string;
      const handlers = this.handlers.get(method) ?? [];
      for (const h of handlers) {
        try {
          h(msg['params']);
        } catch {
          /* subscriber errors must not affect other subscribers */
        }
      }
    }
  }

  /** Send a call that automatically injects vault_id (for notes.* methods). */
  private _call<T>(method: string, params: Record<string, unknown>): Promise<T> {
    if (!this.vaultId) {
      return Promise.reject(new Error('Vault not open; call connect() first'));
    }
    return this._rawCall<T>(method, { vault_id: this.vaultId, ...params });
  }

  /** Send a raw JSON-RPC call without injecting vault_id. */
  private _rawCall<T>(method: string, params: Record<string, unknown>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('WebSocket not connected'));
        return;
      }
      const id = this.nextId++;
      this.pending.set(id, {
        resolve: resolve as (v: unknown) => void,
        reject,
      });
      const req: JsonRpcRequest = { jsonrpc: '2.0', id, method, params };
      this.ws.send(JSON.stringify(req));
    });
  }
}
