/**
 * TanStack Query hook — recent chat message history.
 *
 * Cache key: ["widget", psId, widgetId, "messages"]
 * Fetched exactly once per ChatRenderer mount lifecycle (staleTime: Infinity).
 * The hook is disabled after the first fetch attempt via the `historyFetched`
 * flag in useLiveStore, so it will not re-fire on widget drawer close/reopen.
 */

import { useQuery } from '@tanstack/react-query';
import type { ChatMessage } from '../store/live';

export const HISTORY_LIMIT = 10; // 5 visible turns (user + assistant each)

interface MessagesResponse {
  ps_id: string;
  widget_id: string;
  messages: Array<{ id: string; role: string; text: string; timestamp: string }>;
}

async function fetchRecentMessages(
  psId: string,
  widgetId: string,
): Promise<ChatMessage[]> {
  const res = await fetch(
    `/api/v2/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/messages?limit=${HISTORY_LIMIT}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch messages: ${res.status}`);
  const data: MessagesResponse = await res.json();
  return data.messages.map((m) => ({
    id: m.id,
    role: m.role as ChatMessage['role'],
    text: m.text,
    timestamp: m.timestamp ? Date.parse(m.timestamp) : undefined,
    requestId: m.id,
  }));
}

export function useRecentMessages(psId: string, widgetId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['widget', psId, widgetId, 'messages'],
    queryFn: () => fetchRecentMessages(psId, widgetId),
    enabled: enabled && !!psId && !!widgetId,
    staleTime: Infinity,
    gcTime: 5 * 60 * 1000,
    retry: 1,
  });
}
