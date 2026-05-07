/**
 * draftsStore — unsent chat input, persisted to localStorage.
 *
 * Prevents losing typed text on refresh or drawer close/reopen.
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface DraftsState {
  /** Draft text keyed by widget_id. */
  drafts: Record<string, string>;
  setDraft: (widgetId: string, text: string) => void;
  clearDraft: (widgetId: string) => void;
}

export const useDraftsStore = create<DraftsState>()(
  persist(
    (set) => ({
      drafts: {},
      setDraft: (widgetId, text) =>
        set((s) => ({ drafts: { ...s.drafts, [widgetId]: text } })),
      clearDraft: (widgetId) =>
        set((s) => {
          const next = { ...s.drafts };
          delete next[widgetId];
          return { drafts: next };
        }),
    }),
    { name: 'pyclaego-drafts' },
  ),
);
