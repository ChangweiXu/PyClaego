import { useEffect, useState, useCallback } from 'react';
import type { BacklinkEntry } from './notesRpc';
import { useNotesRpc } from './NotesRpcContext';

interface Props {
  activeRelPath: string | null;
  onClose: () => void;
  /** Called when user clicks a block-anchor badge — parent should scroll to that block. */
  onJumpToBlock: (blockId: string) => void;
  /** Triggered externally (e.g. after a new comment is created) to force a reload. */
  refreshKey?: number;
}

interface CommentGroup {
  blockAnchor: string;
  comments: BacklinkEntry[];
}

export default function CommentsSidebar({
  activeRelPath,
  onClose,
  onJumpToBlock,
  refreshKey = 0,
}: Props) {
  const rpcClient = useNotesRpc();
  const [groups, setGroups] = useState<CommentGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!activeRelPath) { setGroups([]); return; }
    setLoading(true);
    setError(null);
    try {
      const res = await rpcClient.getBacklinks(activeRelPath);
      // Comments are notes stored under _comments/
      const comments = res.backlinks.filter((b) => b.rel_path.startsWith('_comments/'));
      // Group by block_anchor (empty string = no specific anchor)
      const map = new Map<string, BacklinkEntry[]>();
      for (const c of comments) {
        const key = c.block_anchor ?? '';
        if (!map.has(key)) map.set(key, []);
        map.get(key)!.push(c);
      }
      setGroups(
        Array.from(map.entries()).map(([blockAnchor, cs]) => ({ blockAnchor, comments: cs })),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [rpcClient, activeRelPath]);

  useEffect(() => { reload(); }, [reload, refreshKey]);

  return (
    <aside className="notes-comments-sidebar">
      <div className="notes-comments-header">
        <span className="notes-comments-title">💬 Comments</span>
        <div className="notes-comments-header-actions">
          <button className="notes-btn-ghost" onClick={reload} title="Refresh">↺</button>
          <button className="notes-btn-ghost" onClick={onClose} title="Close">✕</button>
        </div>
      </div>

      <div className="notes-comments-body">
        {loading && <div className="notes-comments-state">Loading…</div>}
        {error && <div className="notes-comments-state notes-comments-error">{error}</div>}
        {!loading && !error && groups.length === 0 && (
          <div className="notes-comments-state">No comments yet.</div>
        )}
        {groups.map((g) => (
          <div key={g.blockAnchor || '__unanchored'} className="notes-comment-group">
            {g.blockAnchor && (
              <button
                className="notes-comment-anchor-btn"
                onClick={() => onJumpToBlock(g.blockAnchor)}
                title={`Jump to block ${g.blockAnchor}`}
              >
                ¶ <span className="notes-comment-anchor-id">{g.blockAnchor}</span>
              </button>
            )}
            {g.comments.map((c) => (
              <div key={c.doc_id} className="notes-comment-card">
                <p className="notes-comment-text">{c.snippet || c.title || '(empty)'}</p>
              </div>
            ))}
          </div>
        ))}
      </div>
    </aside>
  );
}
