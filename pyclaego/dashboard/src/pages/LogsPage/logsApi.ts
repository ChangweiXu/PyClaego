// REST client for /api/logs/* endpoints.

export interface LogTreeNode {
  name: string;
  /** Relative path from log_root, e.g. "llm_calls/session-x/foo.json" */
  path: string;
  type: 'file' | 'directory';
  /** bytes — present on files only */
  size?: number;
  /** ISO-8601 — present on files only */
  mtime?: string;
  /** Present on directories only */
  children?: LogTreeNode[];
}

export interface LogFileResponse {
  path: string;
  content: string;
  size: number;
}

export interface LogTreeResponse {
  tree: LogTreeNode[];
}

async function jfetch<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export const logsApi = {
  getTree(): Promise<LogTreeResponse> {
    return jfetch<LogTreeResponse>('/api/logs/tree');
  },

  getFile(path: string): Promise<LogFileResponse> {
    return jfetch<LogFileResponse>(`/api/logs/file?path=${encodeURIComponent(path)}`);
  },
};
