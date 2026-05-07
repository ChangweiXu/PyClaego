export type VaultEventType =
  | 'created' | 'modified' | 'deleted' | 'renamed' | 'index_ready' | 'ping';

export interface VaultEvent {
  type: VaultEventType;
  rel_path: string;
  doc_id?: string;
  title?: string;
  modified_at?: number;
  old_path?: string;
}

type Listener = (event: VaultEvent) => void;



// ── Tabs store ────────────────────────────────────────────────────────────

export type SaveState = 'clean' | 'dirty' | 'saving' | 'error' | 'externally_modified' | 'deleted';

export interface OpenTab {
  rel_path: string;
  title: string;
  content: Record<string, unknown>;
  saveState: SaveState;
}

export type TabsAction =
  | { type: 'open'; rel_path: string; title: string; content: Record<string, unknown> }
  | { type: 'close'; rel_path: string }
  | { type: 'edit'; rel_path: string; content: Record<string, unknown> }
  | { type: 'set_save_state'; rel_path: string; state: SaveState }
  | { type: 'rename'; old_path: string; new_path: string; title: string }
  | { type: 'refresh_content'; rel_path: string; content: Record<string, unknown>; title: string };

export function tabsReducer(
  tabs: OpenTab[],
  action: TabsAction,
): OpenTab[] {
  switch (action.type) {
    case 'open': {
      const existing = tabs.find((t) => t.rel_path === action.rel_path);
      if (existing) return tabs; // already open
      return [...tabs, { rel_path: action.rel_path, title: action.title, content: action.content, saveState: 'clean' }];
    }
    case 'close':
      return tabs.filter((t) => t.rel_path !== action.rel_path);
    case 'edit':
      return tabs.map((t) =>
        t.rel_path === action.rel_path
          ? { ...t, content: action.content, saveState: 'dirty' }
          : t,
      );
    case 'set_save_state':
      return tabs.map((t) =>
        t.rel_path === action.rel_path ? { ...t, saveState: action.state } : t,
      );
    case 'rename':
      return tabs.map((t) =>
        t.rel_path === action.old_path
          ? { ...t, rel_path: action.new_path, title: action.title }
          : t,
      );
    case 'refresh_content':
      return tabs.map((t) =>
        t.rel_path === action.rel_path
          ? { ...t, content: action.content, title: action.title, saveState: 'clean' }
          : t,
      );
    default:
      return tabs;
  }
}
