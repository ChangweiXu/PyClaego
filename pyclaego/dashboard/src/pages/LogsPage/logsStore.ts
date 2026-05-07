// Zustand store for the /dashboard/logs page.

import { create } from 'zustand';
import type { LogTreeNode } from './logsApi';

export interface LogTab {
  /** Unique key — relative path from log_root */
  path: string;
  /** Display label (filename) */
  label: string;
  content: string;
  /** true while the HTTP request is in flight */
  loading: boolean;
  /** HTTP error message, if any */
  error?: string;
}

interface LogsState {
  // ---- tree ------------------------------------------------------------------
  tree: LogTreeNode[];
  /** Paths of currently expanded directory nodes */
  expandedPaths: Set<string>;

  // ---- tabs ------------------------------------------------------------------
  openTabs: LogTab[];
  activeTabPath: string | null;

  // ---- viewer ----------------------------------------------------------------
  wrapLines: boolean;

  // ---- actions ---------------------------------------------------------------
  setTree(tree: LogTreeNode[]): void;
  toggleExpand(path: string): void;
  collapseAll(): void;

  openTab(path: string, label: string): void;
  setTabContent(path: string, content: string): void;
  setTabError(path: string, error: string): void;
  closeTab(path: string): void;
  closeAllTabs(): void;
  setActiveTab(path: string): void;

  toggleWrapLines(): void;
}

export const useLogsStore = create<LogsState>((set, get) => ({
  tree: [],
  expandedPaths: new Set<string>(),
  openTabs: [],
  activeTabPath: null,
  wrapLines: false,

  setTree(tree) {
    set({ tree });
  },

  toggleExpand(path) {
    set((s) => {
      const next = new Set(s.expandedPaths);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return { expandedPaths: next };
    });
  },

  collapseAll() {
    set({ expandedPaths: new Set<string>() });
  },

  openTab(path, label) {
    const existing = get().openTabs.find((t) => t.path === path);
    if (existing) {
      set({ activeTabPath: path });
      return;
    }
    set((s) => ({
      openTabs: [...s.openTabs, { path, label, content: '', loading: true }],
      activeTabPath: path,
    }));
  },

  setTabContent(path, content) {
    set((s) => ({
      openTabs: s.openTabs.map((t) =>
        t.path === path ? { ...t, content, loading: false, error: undefined } : t
      ),
    }));
  },

  setTabError(path, error) {
    set((s) => ({
      openTabs: s.openTabs.map((t) =>
        t.path === path ? { ...t, loading: false, error } : t
      ),
    }));
  },

  closeTab(path) {
    set((s) => {
      const filtered = s.openTabs.filter((t) => t.path !== path);
      let nextActive = s.activeTabPath;
      if (nextActive === path) {
        const idx = s.openTabs.findIndex((t) => t.path === path);
        nextActive =
          filtered.length === 0
            ? null
            : filtered[Math.min(idx, filtered.length - 1)].path;
      }
      return { openTabs: filtered, activeTabPath: nextActive };
    });
  },

  closeAllTabs() {
    set({ openTabs: [], activeTabPath: null });
  },

  setActiveTab(path) {
    set({ activeTabPath: path });
  },

  toggleWrapLines() {
    set((s) => ({ wrapLines: !s.wrapLines }));
  },
}));
