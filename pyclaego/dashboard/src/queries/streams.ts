/**
 * TanStack Query hook — stream history (finished streams from disk JSONL).
 *
 * Cache key: ["widget", psId, widgetId, "streams"]
 * Fetched once per ChatRenderer mount, then disabled via historyFetched flag.
 *
 * Converts raw StreamHistoryChunk arrays into ChatMessage objects that
 * are compatible with the existing message list in liveStore.
 */

import { useQuery } from '@tanstack/react-query';
import { api, type StreamHistoryChunk, type StreamHistoryItem } from '../api';
import type { ChatMessage, ToolCallInfo } from '../store/live';

/**
 * Aggregate raw stream chunks into ChatMessage objects per request_id.
 *
 * Mimics the same accumulation logic as bridge.ts _dispatch:
 *  - thinking_delta HTML → message.text
 *  - text_delta raw → message.text + message.fullContent
 *  - round_separator → message.text
 *  - tool_call_start/end → message.toolCalls
 */
function streamChunksToMessages(
  streams: StreamHistoryItem[],
): ChatMessage[] {
  const result: ChatMessage[] = [];

  for (const stream of streams) {
    const { request_id: requestId, chunks } = stream;
    if (!chunks || chunks.length === 0) continue;

    // Sort by seq (belt-and-suspenders: disk-order should already be correct)
    const sorted = [...chunks].sort((a, b) => (a.seq ?? a._seq ?? 0) - (b.seq ?? b._seq ?? 0));

    let text = '';
    let fullContent = '';
    let lastRoundText = '';
    let currentRound: number | undefined;
    const toolCalls: ToolCallInfo[] = [];
    const seenToolIds = new Set<string>();

    for (const c of sorted) {
      const ct = c.chunk_type;
      const content = c.content ?? '';

      switch (ct) {
        case 'thinking_delta':
          // thinking content already has <details> wrapping from backend
          text += content;
          break;

        case 'text_delta':
          text += content;
          fullContent += content;
          lastRoundText += content;
          break;

        case 'round_separator':
          text += content;
          lastRoundText = '';  // reset for next round
          if (c.round !== undefined) {
            currentRound = c.round;
          }
          break;

        case 'tool_call_start':
          if (c.tool_call_id && c.tool_call_name && !seenToolIds.has(c.tool_call_id)) {
            seenToolIds.add(c.tool_call_id);
            toolCalls.push({
              id: c.tool_call_id,
              name: c.tool_call_name,
              status: 'running',
            });
          }
          break;

        case 'tool_call_end':
          if (c.tool_call_id) {
            const existing = toolCalls.find((tc) => tc.id === c.tool_call_id);
            if (existing) {
              existing.status = 'done';
            }
          }
          break;

        default:
          // Ignore unknown chunk types
          break;
      }
    }

    // Determine timestamp from first chunk
    const firstTs = sorted[0]?.timestamp;
    const timestamp = firstTs ? Date.parse(firstTs) : undefined;

    const displayText = lastRoundText || fullContent || undefined;

    result.push({
      id: `hist-${requestId}`,
      role: 'assistant',
      text,
      displayText,
      requestId,
      streaming: false,
      timestamp: isNaN(timestamp as number) ? undefined : timestamp,
      ...(toolCalls.length > 0 ? { toolCalls } : {}),
      ...(currentRound !== undefined ? { currentRound } : {}),
      ...(fullContent ? { fullContent } : {}),
    });
  }

  return result;
}

export function useWidgetStreams(
  psId: string,
  widgetId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ['widget', psId, widgetId, 'streams'],
    queryFn: async () => {
      const data = await api.getWidgetStreams(psId, widgetId);
      return streamChunksToMessages(data.streams);
    },
    enabled: enabled && !!psId && !!widgetId,
    staleTime: Infinity,
    gcTime: 5 * 60 * 1000,
    retry: 1,
  });
}
