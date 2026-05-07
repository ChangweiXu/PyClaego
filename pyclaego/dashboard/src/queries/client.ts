import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,       // 30s — fresh window before background refetch
      gcTime: 5 * 60_000,      // 5min — keep unused entries in memory
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});
