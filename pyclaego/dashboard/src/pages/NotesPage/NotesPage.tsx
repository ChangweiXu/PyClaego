import { useState, useReducer, useCallback, useEffect, useRef, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { NoteRpcClient, type TagInfo, type SearchResult, type GraphData, type DocMeta } from './notesRpc';
import { tabsReducer, type OpenTab } from './notesStore';
import { NotesRpcContext, useNotesRpc } from './NotesRpcContext';
import FileTree from './FileTree';
import TiptapEditor from './TiptapEditor';
import ChatPanel from './ChatPanel';
import GraphView from './GraphView';
import CommentsSidebar from './CommentsSidebar';
import LibrarianPanel from './LibrarianPanel';
import type { FileTreeNode } from './notesRpc';
import './notes.css';

type DrawerType = 'search' | 'graph' | 'tags' | null;
type RightPanelType = 'chat' | 'comments' | null;

/** Check if a string looks like a UUID v4 doc_id. */
function _looksLikeDocId(s: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s);
}

export default function NotesPage() {
  const { psId, widgetId } = useParams<{ psId: string; widgetId: string }>();
  const ps = psId!;
  const wid = widgetId!;

  // One RPC client per widget, stable across renders
  const [rpcClient] = useState(() => new NoteRpcClient(ps, wid));
  useEffect(() => {
    rpcClient.connect();
    return () => rpcClient.disconnect();
  }, [rpcClient]);

  // Sidebar
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [drawer, setDrawer] = useState<DrawerType>(null);
  const [librarianOpen, setLibrarianOpen] = useState(false);
  // Right panel
  const [rightPanel, setRightPanel] = useState<RightPanelType>(null);
  const [commentsRefreshKey, setCommentsRefreshKey] = useState(0);

  // File tree
  const [tree, setTree] = useState<FileTreeNode[]>([]);

  // Tabs
  const [tabs, dispatch] = useReducer(tabsReducer, []);
  const [activeRelPath, setActiveRelPath] = useState<string | null>(null);

  // Scroll targets: relPath → blockId to scroll to after tab opens
  const [scrollTargets, setScrollTargets] = useState<Record<string, string>>({});

  // Auto-save debounce
  const saveTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // Count of our own pending writes per path, so vault 'modified' events from our
  // own saves don't get flagged as 'externally_modified'.
  const ownWriteCount = useRef<Map<string, number>>(new Map());

  // Fetch tree on mount
  const refreshTree = useCallback(async () => {
    try {
      const res = await rpcClient.listFiles();
      setTree(res.tree);
    } catch (e) {
      console.error('Failed to load file tree', e);
    }
  }, [rpcClient]);

  // Load file tree once the vault is open (fires on every connect / reconnect)
  useEffect(() => {
    return rpcClient.onNotification('vault.opened', () => { refreshTree(); });
  }, [rpcClient, refreshTree]);

  // Vault events via RPC client
  useEffect(() => {
    return rpcClient.onVaultEvent((event) => {
      if (event.type === 'index_ready') { refreshTree(); return; }
      if (event.type === 'created') refreshTree();
      if (event.type === 'deleted') {
        dispatch({ type: 'set_save_state', rel_path: event.rel_path, state: 'deleted' });
        refreshTree();
      }
      if (event.type === 'modified') {
        const pending = ownWriteCount.current.get(event.rel_path) ?? 0;
        if (pending > 0) {
          if (pending <= 1) ownWriteCount.current.delete(event.rel_path);
          else ownWriteCount.current.set(event.rel_path, pending - 1);
          refreshTree();
          return;
        }
        dispatch({ type: 'set_save_state', rel_path: event.rel_path, state: 'externally_modified' });
        refreshTree();
      }
      if (event.type === 'renamed' && event.old_path) {
        dispatch({ type: 'rename', old_path: event.old_path, new_path: event.rel_path, title: event.title || event.rel_path.split('/').pop() || '' });
        refreshTree();
      }
    });
  }, [rpcClient, refreshTree]);

  // Open file in editor
  const openFile = useCallback(async (relPath: string) => {
    const already = tabs.find((t) => t.rel_path === relPath);
    if (already) { setActiveRelPath(relPath); return; }
    try {
      const res = await rpcClient.readFile(relPath);
      dispatch({ type: 'open', rel_path: relPath, title: res.meta?.title || relPath.split('/').pop() || relPath, content: res.content });
      setActiveRelPath(relPath);
    } catch (e) {
      console.error('Failed to open file', e);
    }
  }, [rpcClient, tabs]);

  /** Open a file and, once the tab is active, scroll to the given blockId.
   *  The `pathOrId` may be a rel_path or a doc_id (UUID). */
  const openFileAtBlock = useCallback(async (pathOrId: string, blockId?: string) => {
    // If the target looks like a UUID doc_id, resolve it to a rel_path
    let relPath = pathOrId;
    if (_looksLikeDocId(pathOrId)) {
      try {
        const info = await rpcClient.resolveDocId(pathOrId);
        relPath = info.rel_path;
      } catch {
        console.error('Failed to resolve doc_id', pathOrId);
        return;
      }
    }
    await openFile(relPath);
    if (blockId) {
      setScrollTargets((prev) => ({ ...prev, [relPath]: blockId }));
    }
  }, [openFile, rpcClient]);

  // Handle content edit with debounced auto-save
  const handleEdit = useCallback((relPath: string, content: Record<string, unknown>) => {
    dispatch({ type: 'edit', rel_path: relPath, content });
    const existing = saveTimers.current.get(relPath);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => {
      saveTimers.current.delete(relPath);
      saveFile(relPath, content);
    }, 1500);
    saveTimers.current.set(relPath, timer);
  }, []);

  async function saveFile(relPath: string, content: Record<string, unknown>) {
    dispatch({ type: 'set_save_state', rel_path: relPath, state: 'saving' });
    // Register our own write so the vault event doesn't trigger 'externally_modified'.
    ownWriteCount.current.set(relPath, (ownWriteCount.current.get(relPath) ?? 0) + 1);
    try {
      await rpcClient.writeFile(relPath, content);
      dispatch({ type: 'set_save_state', rel_path: relPath, state: 'clean' });
    } catch (e) {
      console.error('Save failed', e);
      dispatch({ type: 'set_save_state', rel_path: relPath, state: 'error' });
      // Write failed — the vault event won't fire, so undo the counter.
      const cnt = ownWriteCount.current.get(relPath) ?? 0;
      if (cnt <= 1) ownWriteCount.current.delete(relPath);
      else ownWriteCount.current.set(relPath, cnt - 1);
    }
  }

  const handleReloadTab = useCallback(async (relPath: string) => {
    try {
      const res = await rpcClient.readFile(relPath);
      dispatch({ type: 'refresh_content', rel_path: relPath, content: res.content, title: res.meta?.title || relPath.split('/').pop() || '' });
    } catch { /* ignore */ }
  }, [rpcClient]);

  // Create new file
  const handleNewTab = useCallback(async () => {
    const name = prompt('New file path (relative, e.g. ideas/my-note.bdx):');
    if (!name) return;
    const relPath = name.endsWith('.bdx') ? name : name + '.bdx';
    try {
      await rpcClient.createFile(relPath);
      await refreshTree();
      await openFile(relPath);
    } catch (e) {
      alert(`Error: ${e}`);
    }
  }, [rpcClient, refreshTree, openFile]);

  const handleCreateFileInDir = useCallback(async (parentDir: string) => {
    const name = prompt(`New file name in "${parentDir}":`);
    if (!name) return;
    const rel = `${parentDir}/${name.endsWith('.bdx') ? name : name + '.bdx'}`;
    try {
      await rpcClient.createFile(rel);
      await refreshTree();
      await openFile(rel);
    } catch (e) { alert(`Error: ${e}`); }
  }, [rpcClient, refreshTree, openFile]);

  const handleDeleteFile = useCallback(async (relPath: string) => {
    if (!confirm(`Delete "${relPath}"? This cannot be undone.`)) return;
    try {
      await rpcClient.deleteFile(relPath);
      dispatch({ type: 'close', rel_path: relPath });
      if (activeRelPath === relPath) setActiveRelPath(tabs.find((t) => t.rel_path !== relPath)?.rel_path ?? null);
      await refreshTree();
    } catch (e) { alert(`Error: ${e}`); }
  }, [rpcClient, activeRelPath, tabs, refreshTree]);

  const handleRenameFile = useCallback(async (relPath: string) => {
    const newPath = prompt('New path:', relPath);
    if (!newPath || newPath === relPath) return;
    try {
      await rpcClient.renameFile(relPath, newPath.endsWith('.bdx') ? newPath : newPath + '.bdx');
      await refreshTree();
    } catch (e) { alert(`Error: ${e}`); }
  }, [rpcClient, refreshTree]);

  const toggleDrawer = (d: DrawerType) => setDrawer((prev) => prev === d ? null : d);

  return (
    <NotesRpcContext.Provider value={rpcClient}>
    <div className="notes-page" onClick={() => {}}>
      {/* Sidebar toggle when hidden */}
      {!sidebarOpen && (
        <button className="notes-sidebar-toggle" onClick={() => setSidebarOpen(true)}>▶ Show</button>
      )}

      {/* Sidebar */}
      <aside className={`notes-sidebar${sidebarOpen ? '' : ' hidden'}`}>
        <div className="notes-sidebar-top">
          <button className="notes-btn-ghost" onClick={() => toggleDrawer('search')}>🔍 Search</button>
          <button className="notes-btn-ghost" onClick={() => toggleDrawer('graph')}>🕸 Graph</button>
          <button className="notes-btn-ghost" onClick={() => toggleDrawer('tags')}>🏷 Tags</button>
          <button className={`notes-btn-ghost${rightPanel === 'comments' ? ' active' : ''}`} onClick={() => setRightPanel((v) => v === 'comments' ? null : 'comments')}>💬</button>
          <button className={`notes-btn-ghost${rightPanel === 'chat' ? ' active' : ''}`} onClick={() => setRightPanel((v) => v === 'chat' ? null : 'chat')}>🤖</button>
          <button className="notes-btn-ghost" style={{ marginLeft: 'auto' }} onClick={() => setSidebarOpen(false)}>◀ Hide</button>
        </div>
        <div className="notes-sidebar-body">
          <FileTree
            tree={tree}
            activeRelPath={activeRelPath ?? undefined}
            onFileClick={openFile}
            onCreateFile={handleCreateFileInDir}
            onDeleteFile={handleDeleteFile}
            onRenameFile={handleRenameFile}
          />
        </div>
        <div className="notes-sidebar-bottom">
          <button className="notes-btn" style={{ width: '100%' }} onClick={() => setLibrarianOpen(true)}>
            🤖 Librarian
          </button>
        </div>
      </aside>

      {/* Editor */}
      <main className="notes-main">
        <TiptapEditor
          tabs={tabs}
          activeRelPath={activeRelPath}
          psId={ps}
          widgetId={wid}
          onTabClick={setActiveRelPath}
          onTabClose={(rel) => {
            dispatch({ type: 'close', rel_path: rel });
            if (activeRelPath === rel) setActiveRelPath(tabs.find((t) => t.rel_path !== rel)?.rel_path ?? null);
          }}
          onEdit={handleEdit}
          onNewTab={handleNewTab}
          onReloadTab={handleReloadTab}
          onOpenInTab={openFileAtBlock}
          scrollTargets={scrollTargets}
          onScrollComplete={(relPath) =>
            setScrollTargets((prev) => {
              const next = { ...prev };
              delete next[relPath];
              return next;
            })
          }
          onCommentCreated={() => {
            setCommentsRefreshKey((k) => k + 1);
            setRightPanel('comments');
          }}
          rpcClient={rpcClient}
        />
      </main>

      {/* Right panel — Chat or Comments */}
      {rightPanel === 'chat' && (
        <ChatPanel
          psId={ps}
          widgetId={wid}
          activeRelPath={activeRelPath}
        />
      )}
      {rightPanel === 'comments' && (
        <CommentsSidebar
          activeRelPath={activeRelPath}
          onClose={() => setRightPanel(null)}
          onJumpToBlock={(blockId) => {
            const el = document.querySelector(`[data-id="${blockId}"]`);
            el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }}
          refreshKey={commentsRefreshKey}
        />
      )}

      {/* Right-side drawers */}
      {drawer && (
        <div className="notes-drawer-overlay">
          <div className="notes-drawer">
            <div className="notes-drawer-header">
              {drawer === 'search' && '🔍 Search'}
              {drawer === 'graph' && '🕸 Document Graph'}
              {drawer === 'tags' && '🏷 Tags'}
              <button className="notes-btn-ghost" onClick={() => setDrawer(null)}>✕</button>
            </div>
            <div className="notes-drawer-body">
              {drawer === 'search' && (
                <SearchDrawer onOpen={openFile} />
              )}
              {drawer === 'graph' && (
                <GraphDrawer onNodeClick={(docId) => { openFileAtBlock(docId); setDrawer(null); }} />
              )}
              {drawer === 'tags' && (
                <TagsDrawer onDocClick={(rel) => { openFile(rel); setDrawer(null); }} />
              )}
            </div>
          </div>
        </div>
      )}

      {/* Librarian */}
      {librarianOpen && (
        <LibrarianPanel psId={ps} widgetId={wid} onClose={() => setLibrarianOpen(false)} />
      )}
    </div>
    </NotesRpcContext.Provider>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────

function SearchDrawer({ onOpen }: { onOpen: (rel: string) => void }) {
  const rpcClient = useNotesRpc();
  const [q, setQ] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);

  async function doSearch() {
    if (!q.trim()) return;
    setLoading(true);
    try {
      const res = await rpcClient.search(q);
      setResults(res.results);
    } finally { setLoading(false); }
  }

  return (
    <>
      <input
        className="notes-search-input"
        placeholder="Search notes…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && doSearch()}
        autoFocus
      />
      <button className="notes-btn" onClick={doSearch} disabled={loading}>
        {loading ? 'Searching…' : 'Search'}
      </button>
      <div style={{ marginTop: 12 }}>
        {results.map((r) => (
          <div key={r.doc_id} className="notes-search-result" onClick={() => onOpen(r.rel_path)}>
            <div className="notes-search-result-title">{r.title || r.rel_path.split('/').pop()}</div>
            <div className="notes-search-result-path">{r.rel_path}</div>
            <div
              className="notes-search-result-snippet"
              dangerouslySetInnerHTML={{ __html: r.snippet }}
            />
          </div>
        ))}
        {results.length === 0 && q && !loading && <p style={{ color: '#888', fontSize: 13 }}>No results</p>}
      </div>
    </>
  );
}

function GraphDrawer({ onNodeClick }: { onNodeClick: (docId: string) => void }) {
  const rpcClient = useNotesRpc();
  const [data, setData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    rpcClient.graph().then(setData).finally(() => setLoading(false));
  }, [rpcClient]);

  if (loading) return <div>Loading graph…</div>;
  if (!data) return <div>Failed to load graph.</div>;
  return <GraphView data={data} onNodeClick={onNodeClick} />;
}

function TagsDrawer({ onDocClick }: { onDocClick: (rel: string) => void }) {
  const rpcClient = useNotesRpc();
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [selectedTagId, setSelectedTagId] = useState<number | null>(null);
  const [docs, setDocs] = useState<DocMeta[]>([]);

  useEffect(() => {
    rpcClient.listTags().then((r) => setTags(r.tags));
  }, [rpcClient]);

  useEffect(() => {
    if (selectedTagId === null) { setDocs([]); return; }
    rpcClient.docsByTag(selectedTagId).then((r) => setDocs(r.docs));
  }, [rpcClient, selectedTagId]);

  return (
    <div className="notes-tags-layout">
      <div className="notes-tag-list">
        {tags.map((t) => (
          <div
            key={t.tag_id}
            className={`notes-tag-item${selectedTagId === t.tag_id ? ' active' : ''}`}
            onClick={() => setSelectedTagId(t.tag_id)}
          >
            <span>#{t.tag_name}</span>
            <span className="notes-tag-badge">{t.doc_count}</span>
          </div>
        ))}
        {tags.length === 0 && <p style={{ color: '#888', fontSize: 13, padding: 8 }}>No tags yet</p>}
      </div>
      <div className="notes-tag-docs">
        {docs.map((d) => (
          <div key={d.doc_id} className="notes-doc-item" onClick={() => onDocClick(d.rel_path)}>
            <div style={{ fontWeight: 500, fontSize: 13 }}>{d.title || d.rel_path.split('/').pop()}</div>
            <div style={{ fontSize: 11, color: '#888' }}>{d.rel_path}</div>
          </div>
        ))}
        {selectedTagId !== null && docs.length === 0 && (
          <p style={{ color: '#888', fontSize: 13, padding: 8 }}>No docs with this tag</p>
        )}
      </div>
    </div>
  );
}

function LibrarianWindow({ psId: _psId, widgetId: _wid, onClose: _onClose }: { psId: string; widgetId: string; onClose: () => void }) {
  // Legacy stub — replaced by LibrarianPanel
  return null;
}
