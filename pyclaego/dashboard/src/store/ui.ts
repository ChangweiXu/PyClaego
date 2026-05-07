/**
 * uiStore — pure client-side UI state.
 * No server data lives here.
 */
import { create } from 'zustand';

interface UIState {
  /** Currently open drawer's widget id, or null if closed. */
  openWidgetId: string | null;
  openWidget: (id: string) => void;
  closeWidget: () => void;

  /** Whether the left-side Tasks drawer is open. */
  tasksOpen: boolean;
  openTasks: () => void;
  closeTasks: () => void;

  /** Task node selected in the Tasks drawer detail pane. */
  selectedTaskId: string | null;
  selectTask: (id: string) => void;
  clearSelectedTask: () => void;
}

export const useUIStore = create<UIState>((set) => ({
  openWidgetId: null,
  openWidget: (id) => set({ openWidgetId: id }),
  closeWidget: () => set({ openWidgetId: null }),

  tasksOpen: false,
  openTasks: () => set({ tasksOpen: true }),
  closeTasks: () => set({ tasksOpen: false }),

  selectedTaskId: null,
  selectTask: (id) => set({ selectedTaskId: id }),
  clearSelectedTask: () => set({ selectedTaskId: null }),
}));
