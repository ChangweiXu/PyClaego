/**
 * DocPreviewPopover.tsx
 *
 * A floating popover that renders a read-only BlockNote preview of a linked doc.
 * Shown when the user clicks a bdx:link inside the editor.
 *
 * Props:
 *   relPath   — relative path of the target doc (already resolved)
 *   blockId   — optional block anchor to scroll to (e.g. "b_na03")
 *   anchorEl  — DOM element the popover is anchored to (positioned near it)
 *   psId, widgetId — vault location
 *   onOpenInTab — callback to open the doc in a new editor tab
 *   onClose   — dismiss the popover
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { useCreateBlockNote } from '@blocknote/react';
import { BlockNoteView } from '@blocknote/mantine';
import { xmlToBlocks } from './bdxBridge';
import { useNotesRpc } from './NotesRpcContext';
import type { PartialBlock } from '@blocknote/core';

interface Props {
  relPath: string;
  blockId: string;
  /** DOM element or pre-computed rect to position the popover near. */
  anchorEl: HTMLElement | DOMRect;
  onOpenInTab: (relPath: string, blockId?: string) => void;
  onClose: () => void;
}

// Position the popover near the anchor element, keeping it inside the viewport.
function computePosition(anchor: HTMLElement | DOMRect): { top: number; left: number } {
  const rect = anchor instanceof Element ? anchor.getBoundingClientRect() : anchor;
  const popoverWidth = 600;
  const popoverHeight = 460;
  const margin = 8;

  let left = rect.left;
  let top = rect.bottom + margin;

  // Flip left if overflowing right edge
  if (left + popoverWidth > window.innerWidth - margin) {
    left = Math.max(margin, window.innerWidth - popoverWidth - margin);
  }
  // Flip above if overflowing bottom
  if (top + popoverHeight > window.innerHeight - margin) {
    top = Math.max(margin, rect.top - popoverHeight - margin);
  }

  return { top, left };
}

export default function DocPreviewPopover({
  relPath,
  blockId,
  anchorEl,
  onOpenInTab,
  onClose,
}: Props) {
  const rpcClient = useNotesRpc();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [initialBlocks, setInitialBlocks] = useState<PartialBlock[]>([]);
  const [pos, setPos] = useState(() => computePosition(anchorEl));
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Fetch doc content
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    rpcClient.readFile(relPath)
      .then((res) => {
        if (cancelled) return;
        setTitle(res.meta?.title ?? relPath.split('/').pop() ?? relPath);
        setInitialBlocks(xmlToBlocks(res.content as unknown as string, relPath));
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
  }, [rpcClient, relPath]);

  // Recompute position when anchor or viewport changes
  useEffect(() => {
    setPos(computePosition(anchorEl));
  }, [anchorEl]);

  // Dismiss on Escape or click-outside
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    const onClickOutside = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onClickOutside, true);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onClickOutside, true);
    };
  }, [onClose]);

  const handleOpenInTab = useCallback(() => {
    onOpenInTab(relPath, blockId || undefined);
    onClose();
  }, [relPath, blockId, onOpenInTab, onClose]);

  return (
    <div
      ref={popoverRef}
      className="doc-preview-popover"
      style={{ top: pos.top, left: pos.left }}
      // Prevent clicks inside the popover from bubbling to document (would close it)
      onMouseDown={(e) => e.stopPropagation()}
    >
      {/* Header */}
      <div className="doc-preview-header">
        <span className="doc-preview-title" title={relPath}>
          {title ?? relPath.split('/').pop()}
        </span>
        <div className="doc-preview-actions">
          <button className="notes-btn-ghost doc-preview-open-btn" onClick={handleOpenInTab}>
            ↗ Open
          </button>
          <button className="notes-btn-ghost" onClick={onClose} title="Close preview">
            ✕
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="doc-preview-body">
        {loading && <div className="doc-preview-loading">Loading…</div>}
        {error && <div className="doc-preview-error">⚠ {error}</div>}
        {!loading && !error && (
          <PreviewEditor initialBlocks={initialBlocks} scrollToBlockId={blockId} />
        )}
      </div>
    </div>
  );
}

/** Read-only BlockNote editor for the preview. */
function PreviewEditor({
  initialBlocks,
  scrollToBlockId,
}: {
  initialBlocks: PartialBlock[];
  scrollToBlockId: string;
}) {
  const editor = useCreateBlockNote({
    initialContent: initialBlocks.length > 0 ? initialBlocks : undefined,
  });

  // Scroll to blockId after mount
  const containerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!scrollToBlockId || !containerRef.current) return;
    // BlockNote renders block IDs as data attributes; try to scroll to the element
    const el = containerRef.current.querySelector(`[data-id="${scrollToBlockId}"]`);
    if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }, [scrollToBlockId, initialBlocks]);

  return (
    <div ref={containerRef} className="doc-preview-editor">
      <BlockNoteView editor={editor} editable={false} />
    </div>
  );
}
