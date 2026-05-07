/**
 * agentStreamStore — per-Agent streaming state.
 *
 * Separate from useLiveStore to keep subagent streams from polluting
 * chat message lists.  Keyed by `${psId}:${widgetId}:${requestId}:${subagentId}`.
 */

import { create } from 'zustand';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentChunk {
  seq: number;
  chunk_type?: string;
  content?: string;
  done?: boolean;
  tool_call_name?: string;
  tool_call_id?: string;
  round?: number;
  error_code?: string;
  error_message?: string;
  [k: string]: unknown;
}

export interface AgentStreamState {
  subagentId: string;
  parentAgentId?: string;
  label?: string;
  chunks: AgentChunk[];
  finished: boolean;
}

interface AgentStreamsStore {
  /** Map from composite key to stream state. */
  streams: Record<string, AgentStreamState>;

  /** Append a chunk (creates stream if not exists). */
  appendChunk: (
    psId: string,
    widgetId: string,
    requestId: string,
    subagentId: string,
    chunk: AgentChunk,
  ) => void;

  /** Mark stream finished. */
  endStream: (
    psId: string,
    widgetId: string,
    requestId: string,
    subagentId: string,
  ) => void;

  /** Seed from REST history. */
  seedStream: (
    key: string,
    state: AgentStreamState,
  ) => void;

  /** Clear all streams for a widget. */
  clearWidget: (psId: string, widgetId: string) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeKey(psId: string, widgetId: string, requestId: string, subagentId: string): string {
  return `${psId}:${widgetId}:${requestId}:${subagentId}`;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useAgentStreamStore = create<AgentStreamsStore>((set) => ({
  streams: {},

  appendChunk: (psId, widgetId, requestId, subagentId, chunk) =>
    set((s) => {
      const key = makeKey(psId, widgetId, requestId, subagentId);
      const existing = s.streams[key];
      if (existing) {
        return {
          streams: {
            ...s.streams,
            [key]: {
              ...existing,
              chunks: [...existing.chunks, chunk],
              finished: chunk.done === true || existing.finished,
            },
          },
        };
      }
      return {
        streams: {
          ...s.streams,
          [key]: {
            subagentId,
            chunks: [chunk],
            finished: chunk.done === true,
          },
        },
      };
    }),

  endStream: (psId, widgetId, requestId, subagentId) =>
    set((s) => {
      const key = makeKey(psId, widgetId, requestId, subagentId);
      const existing = s.streams[key];
      if (!existing) return s;
      return {
        streams: {
          ...s.streams,
          [key]: { ...existing, finished: true },
        },
      };
    }),

  seedStream: (key, state) =>
    set((s) => ({
      streams: { ...s.streams, [key]: state },
    })),

  clearWidget: (psId, widgetId) =>
    set((s) => {
      const prefix = `${psId}:${widgetId}:`;
      const next: Record<string, AgentStreamState> = {};
      for (const [k, v] of Object.entries(s.streams)) {
        if (!k.startsWith(prefix)) next[k] = v;
      }
      return { streams: next };
    }),
}));
