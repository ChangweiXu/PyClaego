/**
 * queries/agentStreams.ts — TanStack Query hooks for agent stream REST endpoints.
 */

import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type StreamHistoryChunk, type AgentNode } from '../api';
import { useAgentStreamStore } from '../store/agentStreams';

/** Fetch agent stream list once per widget mount. */
export function useAgentStreamList(psId: string, widgetId: string) {
  return useQuery({
    queryKey: ['agent_streams', psId, widgetId],
    queryFn: () => api.getAgentStreams(psId, widgetId),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

/** Fetch single agent stream chunks (lazy — called when user clicks Stream button). */
export function useAgentStreamChunks(
  psId: string,
  widgetId: string,
  requestId: string | null,
  subagentId: string | null,
) {
  const enabled = !!requestId && !!subagentId;

  const { data, ...rest } = useQuery({
    queryKey: ['agent_stream_chunks', psId, widgetId, requestId, subagentId],
    queryFn: () => api.getAgentStreamChunks(psId, widgetId, requestId!, subagentId!),
    enabled,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  // Seed into agentStreamStore when data arrives
  if (data?.found && data.chunks.length > 0 && requestId && subagentId) {
    const seedKey = `${psId}:${widgetId}:${requestId}:${subagentId}`;
    const existing = useAgentStreamStore.getState().streams[seedKey];
    if (!existing || existing.chunks.length < data.chunks.length) {
      useAgentStreamStore.getState().seedStream(seedKey, {
        subagentId,
        chunks: data.chunks.map((c: StreamHistoryChunk) => ({
          ...c,
          seq: c.seq ?? 0,
        })),
        finished: true,
      });
    }
  }

  return { data, ...rest };
}

// ─────────────────────────────────────────────────────────────────────────
// Hydration: 页面刷新后从 REST API 还原 agentStreamStore
// ─────────────────────────────────────────────────────────────────────────

/**
 * 从 Agent Tree 中提取去重的 {request_id, subagent_id} 对，
 * 通过 REST API 拉取 chunk 历史，seed 到 agentStreamStore。
 *
 * 仅当 store 中尚不存在对应数据时才拉取（WS 实时数据优先）。
 * 用 useRef 跟踪已 hydration 的 key，避免 Tree 轮询触发重复拉取。
 */
export function useAgentStreamHydration(
  psId: string,
  widgetId: string,
  agents: AgentNode[],
) {
  const hydratedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!agents.length) return;

    // 1. 递归展平 Agent Tree → {request_id, subagent_id} 去重
    const pairs: Array<{ requestId: string; subagentId: string }> = [];
    const seen = new Set<string>();

    const flatten = (nodes: AgentNode[]) => {
      for (const n of nodes) {
        const sid = n.subagent_id ?? '_main';
        const k = `${n.request_id}:${sid}`;
        if (!seen.has(k)) {
          seen.add(k);
          pairs.push({ requestId: n.request_id, subagentId: sid });
        }
        if (n.children.length) flatten(n.children);
      }
    };
    flatten(agents);

    // 2. 跳过已 hydration 且 store 中已有数据的 key
    const toFetch = pairs.filter((p) => {
      const seedKey = `${psId}:${widgetId}:${p.requestId}:${p.subagentId}`;
      if (hydratedRef.current.has(seedKey)) return false;
      const existing = useAgentStreamStore.getState().streams[seedKey];
      if (existing?.chunks.length) {
        hydratedRef.current.add(seedKey);
        return false;
      }
      return true;
    });

    if (!toFetch.length) return;

    // 3. 逐对拉取 chunk 历史并 seed（顺序执行，避免并发压后端）
    let cancelled = false;
    const hydrate = async () => {
      for (const p of toFetch) {
        if (cancelled) break;
        const seedKey = `${psId}:${widgetId}:${p.requestId}:${p.subagentId}`;
        try {
          const data = await api.getAgentStreamChunks(
            psId, widgetId, p.requestId, p.subagentId,
          );
          if (cancelled) break;
          if (data?.found && data.chunks.length > 0) {
            useAgentStreamStore.getState().seedStream(seedKey, {
              subagentId: p.subagentId,
              chunks: data.chunks.map((c: StreamHistoryChunk) => ({
                ...c,
                seq: c.seq ?? 0,
              })),
              finished: true,
            });
          }
        } catch {
          // 静默降级：单个 agent 拉取失败不影响其他
        }
        hydratedRef.current.add(seedKey);
      }
    };
    hydrate();

    return () => { cancelled = true; };
  }, [agents, psId, widgetId]);
}
