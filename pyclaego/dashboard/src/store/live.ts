/**
 * liveStore — ephemeral data pushed from WebSocket events.
 *
 * Statuses and streaming chunks live here during a session.
 * When the WS bridge receives a `widget.status_changed` event it ALSO
 * patches the TanStack Query cache (highlight), so components using
 * useWidgetHighlight() don't need to read this store.
 *
 * chat messages and task records also live here, keyed by widgetId, so
 * they survive drawer close/reopen within the same browser session.
 */
import { create } from 'zustand';

export interface StreamChunk {
  widgetId: string;
  requestId: string;
  text: string;
  done: boolean;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'error';
  text: string;
  requestId?: string;
  streaming?: boolean;
  /** Number of image attachments included with this message (for display only). */
  imageCount?: number;
  /** Unix timestamp in ms when the message was created. */
  timestamp?: number;
  /** Active/已完成工具调用列表（流式期间动态追加） */
  toolCalls?: ToolCallInfo[];
  /** 当前轮次号（round_separator 时更新） */
  currentRound?: number;
  /** 完整响应内容（done:True 时由 full_content 字段传入，供 sidebar 展示） */
  fullContent?: string;
  /** 消息列表展示用文本（仅最后一轮 LLM 回答，不含思考/分隔符） */
  displayText?: string;
}

/** 单个工具调用的追踪信息 */
export interface ToolCallInfo {
  id: string;       // tool_call_id
  name: string;     // tool_call_name
  status: 'running' | 'done';
}

export interface ChatTaskItem {
  id: string;
  label: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled';
  startedAt: string;
  duration?: number;
  error?: string;
  _start: number; // internal epoch ms for elapsed calc
}

export interface PendingQueryUI {
  queryId: string;
  sessionId: string;
  prompt: string;
  choices: Array<{ value: string; label: string; description: string }>;
  default?: string;
  timeoutS?: number;
  origin?: string;
  toolName?: string;
}

interface LiveState {
  /** Accumulated streaming text per requestId. */
  streams: Record<string, string>;

  /** Per-widget busy flag (true while stream is in-flight). */
  busy: Record<string, boolean>;

  /** Per-widget chat message history. */
  messages: Record<string, ChatMessage[]>;

  /** Tracks which storeKeys have had their history fetch attempted (even if empty). */
  historyFetched: Set<string>;

  /** Active user-confirmation query per widget (null = no pending query). */
  pendingQueries: Record<string, PendingQueryUI | null>;

  /** Number of additional pending queries waiting in the backend FIFO queue
   * (excluding the active HEAD already shown via pendingQueries). Rendered as
   * a "+N" badge next to the PendingQuery card. */
  pendingQueryQueueDepth: Record<string, number>;

  appendChunk: (chunk: StreamChunk) => void;
  clearStream: (requestId: string) => void;
  setBusy: (widgetId: string, busy: boolean) => void;

  addMessage: (storeKey: string, msg: ChatMessage) => void;
  patchMessage: (widgetId: string, requestId: string, patch: Partial<ChatMessage>) => void;
  seedMessages: (key: string, msgs: ChatMessage[]) => void;

  /** 流式工具调用追踪 */
  addToolCall: (storeKey: string, requestId: string, tc: ToolCallInfo) => void;
  updateToolCall: (storeKey: string, requestId: string, toolCallId: string, patch: Partial<ToolCallInfo>) => void;

  setPendingQuery: (storeKey: string, query: PendingQueryUI | null) => void;
  setPendingQueryDepth: (storeKey: string, depth: number) => void;
}

export const useLiveStore = create<LiveState>((set) => ({
  streams: {},
  busy: {},
  messages: {},
  historyFetched: new Set<string>(),
  pendingQueries: {},
  pendingQueryQueueDepth: {},

  appendChunk: ({ requestId, text, done, widgetId }) =>
    set((s) => ({
      streams: { ...s.streams, [requestId]: (s.streams[requestId] ?? '') + text },
      busy: done ? { ...s.busy, [widgetId]: false } : { ...s.busy, [widgetId]: true },
    })),

  clearStream: (requestId) =>
    set((s) => {
      const next = { ...s.streams };
      delete next[requestId];
      return { streams: next };
    }),

  setBusy: (widgetId, busy) =>
    set((s) => ({ busy: { ...s.busy, [widgetId]: busy } })),

  addMessage: (storeKey, msg) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [storeKey]: [...(s.messages[storeKey] ?? []), { timestamp: Date.now(), ...msg }],
      },
    })),

  seedMessages: (key, msgs) =>
    set((s) => {
      const next = new Set(s.historyFetched);
      next.add(key);

      const existing = s.messages[key] ?? [];

      // Fast path: store empty → just write history
      if (existing.length === 0) {
        return {
          historyFetched: next,
          messages: msgs.length > 0 ? { ...s.messages, [key]: msgs } : s.messages,
        };
      }

      // Merge path: WS messages already arrived → deduplicate by requestId / id
      const merged = [...existing];
      for (const msg of msgs) {
        const isDup = merged.some(
          (m) => (msg.requestId && m.requestId === msg.requestId) || m.id === msg.id,
        );
        if (!isDup) {
          merged.push(msg);
        }
      }

      // Sort by timestamp to preserve chronological order
      merged.sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0));

      return {
        historyFetched: next,
        messages: { ...s.messages, [key]: merged },
      };
    }),

  patchMessage: (widgetId, requestId, patch) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [widgetId]: (s.messages[widgetId] ?? []).map((m) =>
          m.requestId === requestId ? { ...m, ...patch } : m,
        ),
      },
    })),

  setPendingQuery: (storeKey, query) =>
    set((s) => ({
      pendingQueries: { ...s.pendingQueries, [storeKey]: query },
    })),

  setPendingQueryDepth: (storeKey, depth) =>
    set((s) => ({
      pendingQueryQueueDepth: {
        ...s.pendingQueryQueueDepth,
        [storeKey]: Math.max(0, depth),
      },
    })),

  addToolCall: (storeKey, requestId, tc) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [storeKey]: (s.messages[storeKey] ?? []).map((m) =>
          m.requestId === requestId
            ? { ...m, toolCalls: [...(m.toolCalls ?? []), tc] }
            : m,
        ),
      },
    })),

  updateToolCall: (storeKey, requestId, toolCallId, patch) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [storeKey]: (s.messages[storeKey] ?? []).map((m) =>
          m.requestId === requestId
            ? {
                ...m,
                toolCalls: (m.toolCalls ?? []).map((tc) =>
                  tc.id === toolCallId ? { ...tc, ...patch } : tc,
                ),
              }
            : m,
        ),
      },
    })),
}));
