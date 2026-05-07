/**
 * TanStack Query hooks for widget cron triggers.
 *
 * Cache key: ["widget", psId, widgetId, "cron"]
 * Cron config is relatively static; 30s staleTime is enough.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api, type WidgetCronTrigger } from '../api';

export function useWidgetCron(psId: string, widgetId: string) {
  return useQuery({
    queryKey: ['widget', psId, widgetId, 'cron'],
    queryFn: () => api.getWidgetCron(psId, widgetId),
    enabled: !!psId && !!widgetId,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
    retry: 1,
  });
}

export function useUpdateWidgetCron(psId: string, widgetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cron: WidgetCronTrigger[]) => api.updateWidgetCron(psId, widgetId, cron),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['widget', psId, widgetId, 'cron'] });
    },
  });
}
