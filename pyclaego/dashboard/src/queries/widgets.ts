/**
 * TanStack Query hooks — one hook per cache key.
 *
 * Cache key design:
 *   ["widgets", psId]                  → list of WidgetSummary
 *   ["widget", psId, id, "info"]       → WidgetInfo (config/manifest)
 *   ["widget", psId, id, "highlight"]  → highlight dict (status, busy, …)
 *   ["widget", psId, id, "view"]       → ViewSchema
 *   ["tasks", psId, id]                → TaskItem[] for task list primitive
 *
 * Data update policy:
 *   - REST → queryClient.setQueryData   (mutations patch directly)
 *   - WS push → ws/bridge.ts calls queryClient.setQueryData
 *   - Background refetch via staleTime/refetchOnWindowFocus
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import { queryClient } from './client';
import type { ViewSchema } from '../schema/types';

// ---------------------------------------------------------------------------
// Widgets list
// ---------------------------------------------------------------------------

export function useWidgets(psId: string) {
  return useQuery({
    queryKey: ['widgets', psId],
    queryFn: () => api.getPS(psId),
    enabled: !!psId,
    select: (data) => data.widgets,
  });
}

// ---------------------------------------------------------------------------
// Single widget info (config, class, manifest)
// ---------------------------------------------------------------------------

export function useWidgetInfo(psId: string, widgetId: string) {
  return useQuery({
    queryKey: ['widget', psId, widgetId, 'info'],
    queryFn: () => api.getWidget(psId, widgetId),
    enabled: !!psId && !!widgetId,
  });
}

// ---------------------------------------------------------------------------
// Highlight  (status, busy, current_question, …)
// Polled at 30s as belt-and-suspenders behind WS push events.
//
// Bug guard: the REST endpoint has no access to in-flight state, so it never
// returns `busy`. If the WS already pushed `busy: true` into the cache we must
// preserve it — otherwise the poll will silently clear the working indicator
// while the agent is still running.
// ---------------------------------------------------------------------------

export function useWidgetHighlight(psId: string, widgetId: string) {
  return useQuery({
    queryKey: ['widget', psId, widgetId, 'highlight'],
    queryFn: async () => {
      const fresh = await api.getHighlight(psId, widgetId).then((r) => r.highlight);
      // If REST didn't return `busy` but the cache already has busy=true (from
      // a WS push), preserve it so the status dot stays "working".
      if (!('busy' in fresh)) {
        const cached = queryClient.getQueryData<Record<string, unknown>>(
          ['widget', psId, widgetId, 'highlight'],
        );
        if (cached?.['busy'] === true) {
          return { ...fresh, busy: true };
        }
      }
      return fresh;
    },
    enabled: !!psId && !!widgetId,
    refetchInterval: 30_000,   // fallback poll
  });
}

// ---------------------------------------------------------------------------
// ViewSchema  (how the drawer body should render)
// ---------------------------------------------------------------------------

export function useWidgetView(psId: string, widgetId: string) {
  return useQuery({
    queryKey: ['widget', psId, widgetId, 'view'],
    queryFn: () =>
      api.getWidgetView(psId, widgetId).then((r) => r.view as unknown as ViewSchema),
    enabled: !!psId && !!widgetId,
  });
}

// ---------------------------------------------------------------------------
// Command mutation
// ---------------------------------------------------------------------------

export function useWidgetCommand(psId: string, widgetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ command, args }: { command: string; args?: Record<string, unknown> }) =>
      api.sendCommand(psId, widgetId, command, args),
    onSuccess: () => {
      // Invalidate highlight so status dot refreshes quickly after a command.
      qc.invalidateQueries({ queryKey: ['widget', psId, widgetId, 'highlight'] });
    },
  });
}
