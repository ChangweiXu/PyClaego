// REST 客户端：与 /api/v2/* 一一对应。
// 所有请求都走相对路径，通过 Vite proxy（dev）或 FastAPI 同源（生产）。

const BASE = '/api/v2';

async function jfetch<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export interface WidgetClassInfo {
  class_id: string;
  title: string;
  description: string;
  source: string;
  config_schema: Record<string, unknown>;
  has_hook: boolean;
  defaults: Record<string, unknown>;
}

export interface WidgetSummary {
  widget_id: string;
  widget_class: string;
  title: string;
}

export interface PSSummary {
  ps_id: string;
  manifest: Record<string, unknown>;
  widgets: WidgetSummary[];
  loaded: boolean;
}

export interface TaskNode {
  task_id: string;
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
  children?: TaskNode[];
}

/** Backend-built agent tree node (from /api/tasks/agent-tree). */
export interface AgentNode {
  id: string;
  label: string;
  request_id: string;
  subagent_id: string | null;
  task_type: string;
  children: AgentNode[];
}

export interface ArtifactRef {
  artifact_id: string;
  kind: string;
  name: string;
  mime: string;
  size: number;
  ext: string;
  created_at: number;
  extra?: Record<string, unknown>;
}

export interface LLMProvidersInfo {
  providers: string[];
  default_provider: string;
}

export interface WidgetInfo {
  ps_id: string;
  widget_id: string;
  manifest: Record<string, unknown>;
  widget_config: Record<string, unknown>;
  resolved_config: Record<string, unknown>;
  widget_class: {
    class_id: string;
    title: string;
    config_schema: Record<string, unknown>;
    has_hook: boolean;
  } | null;
}

export interface WidgetCronTrigger {
  id: string;
  prompt: string;
  schedule?: string | null;
  interval_seconds?: number | null;
  enabled?: boolean;
  user_id?: string;
  timezone?: string | null;
  params?: Record<string, unknown>;
}

/** 流式消息历史——单个 chunk 记录 */
export interface StreamHistoryChunk {
  seq: number;
  _seq: number;
  type: string;
  chunk_type: string;
  content: string;
  request_id: string;
  ps_id: string;
  widget_id: string;
  done: boolean;
  round?: number;
  tool_call_name?: string;
  tool_call_id?: string;
  full_content?: string;
  timestamp: string;
  source?: string;
}

/** 流式消息历史——单条流 */
export interface StreamHistoryItem {
  request_id: string;
  status: 'finished' | 'active';
  chunks: StreamHistoryChunk[];
}

/** Agent 流摘要（用于 AgentTreePanel 列表） */
export interface AgentStreamSummary {
  request_id: string;
  subagent_id?: string;
  finished: boolean;
  chunk_count?: number;
}

export const api = {
  listWidgetClasses: () =>
    jfetch<{ widget_classes: WidgetClassInfo[]; total: number }>(`${BASE}/widget_classes`),

  getLLMProviders: () =>
    jfetch<LLMProvidersInfo>(`${BASE}/llm_providers`),


  listPersonalSpaces: () =>
    jfetch<{ personal_spaces: string[] }>(`${BASE}/personal_spaces`),

  createOrGetPS: (psId: string, body?: { title?: string; description?: string }) =>
    jfetch<PSSummary>(`${BASE}/personal_spaces/${encodeURIComponent(psId)}`, {
      method: 'POST',
      body: JSON.stringify(body || {}),
    }),

  getPS: (psId: string) =>
    jfetch<PSSummary>(`${BASE}/personal_spaces/${encodeURIComponent(psId)}`),

  createWidget: (
    psId: string,
    body: { widget_id: string; widget_class: string; title?: string; config?: Record<string, unknown> },
  ) =>
    jfetch<{ ok: boolean; widget_id: string }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  getWidget: (psId: string, widgetId: string) =>
    jfetch<WidgetInfo>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}`,
    ),

  updateWidgetConfig: (psId: string, widgetId: string, config: Record<string, unknown>) =>
    jfetch<{ ok: boolean }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/config`,
      { method: 'PATCH', body: JSON.stringify({ config }) },
    ),

  updateWidgetManifest: (psId: string, widgetId: string, manifest: { title?: string }) =>
    jfetch<{ ok: boolean }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}`,
      { method: 'PATCH', body: JSON.stringify(manifest) },
    ),

  deleteWidget: (psId: string, widgetId: string) =>
    jfetch<{ ok: boolean }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}`,
      { method: 'DELETE' },
    ),

  getHighlight: (psId: string, widgetId: string) =>
    jfetch<{ ps_id: string; widget_id: string; highlight: Record<string, unknown> }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/highlight`,
    ),

  getWidgetView: (psId: string, widgetId: string) =>
    jfetch<{ ps_id: string; widget_id: string; view: Record<string, unknown> }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/view`,
    ),

  sendCommand: (psId: string, widgetId: string, command: string, args: Record<string, unknown> = {}) =>
    jfetch<{ ok: boolean; data?: Record<string, unknown>; error?: string }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/commands`,
      { method: 'POST', body: JSON.stringify({ command, args }) },
    ),

  getTasks: () =>
    jfetch<{ sessions: Record<string, { session_id: string; task_count: number; root_tasks: TaskNode[] }>; total_sessions: number; total_tasks: number }>('/api/tasks/snapshot'),

  getAgentTree: (psId: string, widgetId: string) =>
    jfetch<{ ps_id: string; widget_id: string; agents: AgentNode[] }>(
      `/api/tasks/agent-tree/${encodeURIComponent(psId)}/${encodeURIComponent(widgetId)}`,
    ),

  getTaskArtifacts: (taskId: string) =>
    jfetch<{ task_id: string; artifacts: ArtifactRef[]; digest: Record<string, unknown> }>(
      `/api/tasks/tasks/${encodeURIComponent(taskId)}/artifacts`,
    ),

  /** Returns raw text content + mime type. Caller handles display. */
  getTaskArtifactBlob: async (taskId: string, artifactId: string): Promise<{ text: string; mime: string; name: string }> => {
    const resp = await fetch(
      `/api/tasks/artifacts/${encodeURIComponent(taskId)}/${encodeURIComponent(artifactId)}`,
    );
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    const text = await resp.text();
    const mime = resp.headers.get('Content-Type') ?? 'text/plain';
    const name = resp.headers.get('X-Artifact-Name') ?? artifactId;
    return { text, mime, name };
  },

  getWidgetCron: (psId: string, widgetId: string) =>
    jfetch<{ ps_id: string; widget_id: string; cron: WidgetCronTrigger[] }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/cron`,
    ),

  updateWidgetCron: (psId: string, widgetId: string, cron: WidgetCronTrigger[]) =>
    jfetch<{ ok: boolean; count: number }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/cron`,
      { method: 'PUT', body: JSON.stringify({ cron }) },
    ),

  getWidgetHistory: (psId: string, widgetId: string) =>
    jfetch<{ ps_id: string; widget_id: string; history: unknown[] }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/history`,
    ),

  /** 拉取已完成流的历史 chunk（从 JSONL 磁盘文件读取） */
  getWidgetStreams: (psId: string, widgetId: string, requestId?: string) =>
    jfetch<{ ps_id: string; widget_id: string; streams: StreamHistoryItem[] }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/streams${
        requestId ? `?request_id=${encodeURIComponent(requestId)}` : ''
      }`,
    ),

  /** 列出 widget 下所有 Agent 流（含子 Agent） */
  getAgentStreams: (psId: string, widgetId: string) =>
    jfetch<{ ps_id: string; widget_id: string; agents: AgentStreamSummary[] }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/agent_streams`,
    ),

  /** 拉取单个 Agent 流的完整 chunk 历史 */
  getAgentStreamChunks: (psId: string, widgetId: string, requestId: string, subagentId: string) =>
    jfetch<{ ps_id: string; widget_id: string; request_id: string; subagent_id: string; chunks: StreamHistoryChunk[]; found: boolean }>(
      `${BASE}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/agent_streams/${encodeURIComponent(requestId)}/${encodeURIComponent(subagentId)}`,
    ),
};
