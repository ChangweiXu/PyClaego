import { useEffect, useRef, useCallback, useState } from 'react';
import {
  useCreateBlockNote,
  LinkToolbarController,
  FormattingToolbarController,
  FormattingToolbar,
  getFormattingToolbarItems,
} from '@blocknote/react';
import type { LinkToolbarProps } from '@blocknote/react';
import { BlockNoteView } from '@blocknote/mantine';
import '@blocknote/react/style.css';
import '@blocknote/mantine/style.css';
import { xmlToBlocks, blocksToXml, extractTagsFromContent, patchTags } from './bdxBridge';
import DocPreviewPopover from './DocPreviewPopover';
import CustomLinkToolbar from './CustomLinkToolbar';
import { useNotesRpc } from './NotesRpcContext';
import type { OpenTab, SaveState } from './notesStore';

interface Props {
  tabs: OpenTab[];
  activeRelPath: string | null;
  psId: string;
  widgetId: string;
  onTabClick: (relPath: string) => void;
  onTabClose: (relPath: string) => void;
  onEdit: (relPath: string, content: string) => void;
  onNewTab: () => void;
  onReloadTab: (relPath: string) => void;
  onOpenInTab: (relPath: string, blockId?: string) => void;
  /** Called after a comment note is created so the sidebar can refresh. */
  onCommentCreated?: () => void;
  /** Map of relPath → blockId to scroll to after opening a tab. */
  scrollTargets?: Record<string, string>;
  /** Called when a scroll target has been consumed (so parent can clear it). */
  onScrollComplete?: (relPath: string) => void;
}

function saveStateLabel(state: SaveState): { text: string; cls: string } {
  switch (state) {
    case 'dirty': return { text: '● Unsaved', cls: 'dirty' };
    case 'saving': return { text: '⟳ Saving…', cls: 'saving' };
    case 'error': return { text: '✗ Error', cls: 'error' };
    case 'externally_modified': return { text: '⚠ Changed externally', cls: 'warn' };
    case 'deleted': return { text: '✗ Deleted', cls: 'error' };
    default: return { text: 'Saved', cls: 'clean' };
  }
}

// Debounce delay for auto-save (ms)
const AUTOSAVE_DELAY = 1500;

/** Inner editor component per open tab. Kept alive while the tab is open. */
function BlockEditor({
  relPath,
  content,
  isActive,
  isPopoverOpen,
  onEdit,
  onLinkClick,
  onOpenInNote,
  onSelectionChange,
  onCommentClick,
  onCopyLinkClick,
  scrollToBlockId,
  onScrollComplete,
}: {
  relPath: string;
  content: Record<string, unknown> | string;
  isActive: boolean;
  isPopoverOpen: boolean;
  onEdit: (relPath: string, xmlContent: string) => void;
  onLinkClick: (href: string, anchorEl: HTMLElement) => void;
  onOpenInNote: (href: string, anchorEl: HTMLElement) => void;
  onSelectionChange: (blockId: string | null, rect: DOMRect | null) => void;
  onCommentClick: () => void;
  onCopyLinkClick: () => void;
  scrollToBlockId?: string;
  onScrollComplete?: () => void;
}) {
  // Legacy BlockNote editor — content is always XML string in this path
  const contentStr = content as unknown as string;
  const editor = useCreateBlockNote({
    initialContent: xmlToBlocks(contentStr, relPath),
  });

  // Track current block type for topbar active state
  const [curBlockType, setCurBlockType] = useState<string>('paragraph');
  const [curBlockLevel, setCurBlockLevel] = useState<number>(1);
  useEffect(() => {
    const unsub = editor.onEditorSelectionChange(() => {
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const blk = editor.getTextCursorPosition().block as any;
        setCurBlockType(blk.type ?? 'paragraph');
        setCurBlockLevel(blk.props?.level ?? 1);
      } catch { /* editor may not be ready */ }
    });
    return unsub;
  }, [editor]);

  // When the tab is reloaded externally, re-sync editor state
  const prevContentRef = useRef(contentStr);
  useEffect(() => {
    if (contentStr !== prevContentRef.current) {
      prevContentRef.current = contentStr;
      const newBlocks = xmlToBlocks(contentStr, relPath);
      editor.replaceBlocks(editor.document, newBlocks);
    }
  }, [content, editor]);

  // Stable refs to always-current prop values (used inside stable callbacks/effects)
  const contentRef = useRef(contentStr);
  contentRef.current = contentStr;
  const onEditRef = useRef(onEdit);
  onEditRef.current = onEdit;

  // Core save: generate XML, detect inline #tags, patch <bdx:tags> meta, call onEdit.
  // The backend re-indexes tags from the XML on every write, so keeping meta in sync
  // here is sufficient — no separate "add/remove tag" API calls are needed.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveNow = useCallback(() => {
    const xml = blocksToXml(editor.document as Parameters<typeof blocksToXml>[0], contentRef.current);
    const tags = extractTagsFromContent(xml);
    const finalXml = patchTags(xml, tags);
    prevContentRef.current = finalXml;
    onEditRef.current(relPath, finalXml);
  }, [editor, relPath]);

  // Auto-save with debounce — delegates to saveNow
  const handleChange = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(saveNow, AUTOSAVE_DELAY);
  }, [saveNow]);

  // Tag insert popup state
  const [tagPopup, setTagPopup] = useState<{ visible: boolean; input: string }>({ visible: false, input: '' });

  const handleTagInsert = useCallback(() => {
    const raw = tagPopup.input.trim().replace(/^#+/, '');
    setTagPopup({ visible: false, input: '' });
    if (!raw) { editor.focus(); return; }
    try {
      // Insert as styled text (bold) to make it visually distinct as a tag
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (editor as any).insertInlineContent([{ type: 'text', text: `#${raw}`, styles: { bold: true, textColor: '#4f8ef7' } }]);
    } catch {
      // Fallback: insert as a new paragraph block after the current one
      try {
        const b = editor.getTextCursorPosition().block;
        editor.insertBlocks([{ type: 'paragraph', content: [{ type: 'text', text: `#${raw}`, styles: { bold: true } }] }], b, 'after');
      } catch { /* ignore */ }
    }
    editor.focus();
  }, [editor, tagPopup.input]);

  // Stable ref for saveNow so keydown effect captures latest version without re-registering
  const saveNowRef = useRef(saveNow);
  saveNowRef.current = saveNow;

  // Document-level keydown (capture phase, only when this tab is active):
  //   Cmd+S / Ctrl+S → immediate save (prevents browser "Save Page As")
  //   #              → open tag insert popup (when not already typing in an input)
  useEffect(() => {
    if (!isActive) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
        saveNowRef.current();
        return;
      }
      if (e.key === '#' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const target = e.target as HTMLElement;
        if (target.tagName !== 'INPUT' && target.tagName !== 'TEXTAREA') {
          e.preventDefault();
          setTagPopup({ visible: true, input: '' });
        }
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [isActive]);

  // Track text selection and notify parent with block ID + rect
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!isActive) return;
    const handler = () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) {
        onSelectionChange(null, null);
        return;
      }
      const range = sel.getRangeAt(0);
      const el = wrapperRef.current;
      if (!el || !el.contains(range.commonAncestorContainer)) {
        onSelectionChange(null, null);
        return;
      }
      const rect = range.getBoundingClientRect();
      // Resolve the current block from editor state
      try {
        const block = editor.getTextCursorPosition().block;
        onSelectionChange(block.id, rect);
      } catch {
        onSelectionChange(null, null);
      }
    };
    document.addEventListener('selectionchange', handler);
    return () => document.removeEventListener('selectionchange', handler);
  }, [isActive, editor, onSelectionChange]);

  // Stable ref for onOpenInNote so the toolbar closure doesn't need it as a dep
  const onOpenInNoteRef = useRef(onOpenInNote);
  onOpenInNoteRef.current = onOpenInNote;

  const onCommentClickRef = useRef(onCommentClick);
  onCommentClickRef.current = onCommentClick;
  const onCopyLinkClickRef = useRef(onCopyLinkClick);
  onCopyLinkClickRef.current = onCopyLinkClick;

  // Stable custom toolbar component (empty deps, reads latest callback via ref)
  const CustomToolbarComp = useCallback(
    (props: LinkToolbarProps) => (
      <CustomLinkToolbar
        {...props}
        onOpenInNote={(href, el) => onOpenInNoteRef.current(href, el)}
      />
    ),
    [], // intentionally empty — uses ref above
  );

  // Merged formatting toolbar: BlockNote default items + Comment + Copy link buttons
  const MergedToolbarComp = useCallback(
    () => (
      <FormattingToolbar>
        {[
          ...getFormattingToolbarItems(),
          <button
            key="sel-comment"
            className="notes-sel-btn notes-sel-btn-toolbar"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => onCommentClickRef.current()}
          >💬</button>,
          <button
            key="sel-copy-link"
            className="notes-sel-btn notes-sel-btn-toolbar"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => onCopyLinkClickRef.current()}
          >🔗</button>,
        ]}
      </FormattingToolbar>
    ),
    [], // intentionally empty — reads latest callbacks via refs
  );

  // Scroll to a block once the editor is active and the block is in the DOM
  const onScrollCompleteRef = useRef(onScrollComplete);
  onScrollCompleteRef.current = onScrollComplete;
  useEffect(() => {
    if (!isActive || !scrollToBlockId) return;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tryScroll = () => {
      const el = wrapperRef.current?.querySelector(`[data-id="${scrollToBlockId}"]`);
      if (el) {
        el.scrollIntoView({ block: 'start', behavior: 'smooth' });
        onScrollCompleteRef.current?.();
      } else if (attempts < 15) {
        attempts++;
        timer = setTimeout(tryScroll, 80);
      }
    };
    requestAnimationFrame(tryScroll);
    return () => { if (timer) clearTimeout(timer); };
  }, [isActive, scrollToBlockId]);

  // Intercept link clicks inside the editor DOM via event delegation.
  //
  // WHY TWO LISTENERS:
  //   @tiptap/extension-link's ProseMirror clickHandler plugin calls window.open()
  //   from a `mouseup` listener registered on the *document* (bubble phase). A
  //   capture-phase `click` handler on the wrapper fires too late — window.open has
  //   already been called. We therefore add a document-level capture-phase `mouseup`
  //   handler which fires BEFORE ProseMirror's document bubble handler, and stops
  //   propagation so ProseMirror never calls window.open.  The click event is a
  //   separate synthetic event and still fires afterwards — our click handler below
  //   then opens the preview popover.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    // 1. mouseup capture on document — fires before ProseMirror's document-level
    //    mouseup (bubble phase) so window.open is never reached.
    const onMouseUp = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      const anchor = target.closest?.('a') as HTMLAnchorElement | null;
      if (!anchor || !wrapper.contains(anchor)) return;
      const rawHref = anchor.getAttribute('href') ?? '';
      if (!rawHref) return;
      // Only intercept bdx / same-origin links; let truly external links through.
      if (rawHref.startsWith('https://') && !rawHref.startsWith(window.location.origin)) return;
      if (rawHref.startsWith('http://') && !rawHref.startsWith(window.location.origin)) return;
      e.stopImmediatePropagation(); // blocks ProseMirror's document mouseup → no window.open
    };
    document.addEventListener('mouseup', onMouseUp, true);

    // 2. click capture on wrapper — opens the preview popover.
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      const anchor = target.closest('a') as HTMLAnchorElement | null;
      if (!anchor) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      const rawHref = anchor.getAttribute('href') ?? '';
      if (!rawHref) return;
      let href = rawHref;
      // BlockNote may normalize relative hrefs to absolute same-origin URLs.
      if (href.startsWith(window.location.origin)) {
        href = href.slice(window.location.origin.length).replace(/^\//, '');
      }
      // Truly external link → open in new tab.
      if (href.startsWith('http://') || href.startsWith('https://')) {
        window.open(href, '_blank', 'noopener,noreferrer');
        return;
      }
      onLinkClick(href, anchor);
    };
    wrapper.addEventListener('click', onClick, true);

    return () => {
      document.removeEventListener('mouseup', onMouseUp, true);
      wrapper.removeEventListener('click', onClick, true);
    };
  }, [onLinkClick]);

  return (
    <div
      ref={wrapperRef}
      className={`notes-blocknote-wrapper${isPopoverOpen ? ' has-popover' : ''}`}
      style={{ display: isActive ? 'flex' : 'none', flexDirection: 'column', flex: 1, overflow: 'auto' }}
    >
      {/* Always-visible formatting / insert topbar */}
      <div className="notes-editor-topbar">
        <div className="notes-topbar-group">
          <button
            className={`notes-topbar-btn${curBlockType === 'paragraph' ? ' active' : ''}`}
            title="Paragraph"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { try { const b = editor.getTextCursorPosition().block; editor.updateBlock(b, { type: 'paragraph' }); editor.focus(); } catch {} }}
          >¶</button>
          <button
            className={`notes-topbar-btn${curBlockType === 'heading' && curBlockLevel === 1 ? ' active' : ''}`}
            title="Heading 1"
            onMouseDown={(e) => e.preventDefault()}
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            onClick={() => { try { const b = editor.getTextCursorPosition().block; editor.updateBlock(b, { type: 'heading', props: { level: 1 } } as any); editor.focus(); } catch {} }}
          >H1</button>
          <button
            className={`notes-topbar-btn${curBlockType === 'heading' && curBlockLevel === 2 ? ' active' : ''}`}
            title="Heading 2"
            onMouseDown={(e) => e.preventDefault()}
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            onClick={() => { try { const b = editor.getTextCursorPosition().block; editor.updateBlock(b, { type: 'heading', props: { level: 2 } } as any); editor.focus(); } catch {} }}
          >H2</button>
          <button
            className={`notes-topbar-btn${curBlockType === 'heading' && curBlockLevel === 3 ? ' active' : ''}`}
            title="Heading 3"
            onMouseDown={(e) => e.preventDefault()}
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            onClick={() => { try { const b = editor.getTextCursorPosition().block; editor.updateBlock(b, { type: 'heading', props: { level: 3 } } as any); editor.focus(); } catch {} }}
          >H3</button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Insert bullet list item"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { try { const b = editor.getTextCursorPosition().block; editor.insertBlocks([{ type: 'bulletListItem' }], b, 'after'); editor.focus(); } catch {} }}
          >• List</button>
          <button
            className="notes-topbar-btn"
            title="Insert numbered list item"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { try { const b = editor.getTextCursorPosition().block; editor.insertBlocks([{ type: 'numberedListItem' }], b, 'after'); editor.focus(); } catch {} }}
          >1. List</button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className={`notes-topbar-btn${tagPopup.visible ? ' active' : ''}`}
            title="Insert tag (#tag) — also triggered by typing #"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              if (tagPopup.visible) {
                setTagPopup({ visible: false, input: '' });
                editor.focus();
              } else {
                setTagPopup({ visible: true, input: '' });
              }
            }}
          >#</button>
        </div>
      </div>
      {/* Tag insert popup — shown when # button is active or # key pressed */}
      {tagPopup.visible && (
        <div className="notes-tag-insert-popup">
          <span className="notes-tag-insert-hash">#</span>
          <input
            autoFocus
            className="notes-tag-insert-input"
            placeholder="tag name…"
            value={tagPopup.input}
            onChange={(e) => setTagPopup((p) => ({ ...p, input: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); handleTagInsert(); }
              if (e.key === 'Escape') {
                e.preventDefault();
                setTagPopup({ visible: false, input: '' });
                editor.focus();
              }
            }}
          />
          <button
            className="notes-topbar-btn"
            onMouseDown={(e) => e.preventDefault()}
            onClick={handleTagInsert}
          >Insert</button>
          <button
            className="notes-topbar-btn"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { setTagPopup({ visible: false, input: '' }); editor.focus(); }}
          >✕</button>
        </div>
      )}
      {/* linkToolbar={false} disables BlockNote's default link toolbar.
          formattingToolbar={false} disables the default floating style toolbar.
          We render our own controllers as children instead. */}
      <BlockNoteView editor={editor} onChange={handleChange} linkToolbar={false} formattingToolbar={false}>
        <LinkToolbarController linkToolbar={CustomToolbarComp} />
        <FormattingToolbarController formattingToolbar={MergedToolbarComp} />
      </BlockNoteView>
    </div>
  );
}

export default function EditorPane({
  tabs,
  activeRelPath,
  psId,
  widgetId,
  onTabClick,
  onTabClose,
  onEdit,
  onNewTab,
  onCommentCreated,
  onReloadTab,
  onOpenInTab,
  scrollTargets,
  onScrollComplete,
}: Props) {
  const activeTab = tabs.find((t) => t.rel_path === activeRelPath);

  // Selection toolbar state
  const [selInfo, setSelInfo] = useState<{ blockId: string; rect: DOMRect } | null>(null);
  // Comment input state (shown when user clicks "💬 Comment" in the toolbar)
  const [commentState, setCommentState] = useState<'hidden' | 'input' | 'submitting'>('hidden');
  const [commentText, setCommentText] = useState('');

  const handleSelectionChange = useCallback((blockId: string | null, rect: DOMRect | null) => {
    if (!blockId || !rect) { setSelInfo(null); return; }
    setSelInfo({ blockId, rect });
  }, []);

  const handleCopyBlockLink = useCallback(() => {
    if (!selInfo || !activeRelPath) return;
    const link = `${activeRelPath}#${selInfo.blockId}`;
    navigator.clipboard.writeText(link).catch(() => {});
    setSelInfo(null);
  }, [selInfo, activeRelPath]);

  /** Called from the merged FormattingToolbar "💬" button */
  const handleCommentBtnClick = useCallback(() => {
    setCommentState('input');
  }, []);

  /** Called from the merged FormattingToolbar "🔗" button */
  const handleCopyLinkBtnClick = useCallback(() => {
    handleCopyBlockLink();
  }, [handleCopyBlockLink]);

  const rpcClient = useNotesRpc();

  const handleCommentSubmit = useCallback(async () => {
    if (!selInfo || !activeRelPath || !commentText.trim()) return;
    setCommentState('submitting');
    try {
      await rpcClient.createComment(activeRelPath, selInfo.blockId, commentText.trim());
      onCommentCreated?.();
    } catch (e) {
      console.error('Failed to create comment', e);
    } finally {
      setCommentText('');
      setCommentState('hidden');
      setSelInfo(null);
    }
}, [selInfo, activeRelPath, commentText, rpcClient, onCommentCreated]);

  // Popover state
  const [popover, setPopover] = useState<{
    relPath: string;
    blockId: string;
    anchorEl: HTMLElement;
  } | null>(null);

  /** Resolve a raw bdx href to { relPath, blockId } relative to the active note. */
  const resolveHref = useCallback((href: string): { relPath: string; blockId: string } => {
    const hashIdx = href.lastIndexOf('#');
    const target = hashIdx > 0 ? href.slice(0, hashIdx) : href;
    const blockId = hashIdx > 0 ? href.slice(hashIdx + 1) : '';
    const fromPath = activeRelPath ?? '';
    const fromDir = fromPath.includes('/') ? fromPath.slice(0, fromPath.lastIndexOf('/') + 1) : '';
    const resolved = target.startsWith('/') ? target.slice(1) : fromDir + target;
    return { relPath: resolved, blockId };
  }, [activeRelPath]);

  /** Click on link text → show preview popover */
  const handleLinkClick = useCallback((href: string, anchorEl: HTMLElement) => {
    const { relPath, blockId } = resolveHref(href);
    setPopover({ relPath, blockId, anchorEl });
  }, [resolveHref]);

  /** Hover toolbar "↗ Open" → open note in tab directly (no popover) */
  const handleOpenInNote = useCallback((href: string, _anchorEl: HTMLElement) => {
    const { relPath, blockId } = resolveHref(href);
    onOpenInTab(relPath, blockId || undefined);
  }, [resolveHref, onOpenInTab]);

  return (
    <div className="notes-editor-pane">
      {/* Tab bar */}
      <div className="notes-tabbar">
        {tabs.map((tab) => {
          const { text, cls } = saveStateLabel(tab.saveState);
          const isActive = tab.rel_path === activeRelPath;
          return (
            <div
              key={tab.rel_path}
              className={`notes-tab${isActive ? ' active' : ''}`}
              onClick={() => onTabClick(tab.rel_path)}
            >
              <span className="notes-tab-title">{tab.title || tab.rel_path.split('/').pop()}</span>
              <span className={`notes-tab-state ${cls}`}>{text}</span>
              <button
                className="notes-tab-close"
                onClick={(e) => { e.stopPropagation(); onTabClose(tab.rel_path); }}
                title="Close tab"
              >×</button>
            </div>
          );
        })}
        <button className="notes-btn-ghost notes-tab-new" onClick={onNewTab} title="New file">＋</button>
      </div>

      {tabs.length > 0 ? (
        <div className="notes-editor-body" style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          {/* Reload banner when externally modified */}
          {activeTab && (activeTab.saveState === 'externally_modified' || activeTab.saveState === 'deleted') && (
            <div className="notes-editor-toolbar">
              <button
                className="notes-btn-warn"
                onClick={() => onReloadTab(activeTab.rel_path)}
              >
                ↺ Reload from disk
              </button>
            </div>
          )}
          {/* Render all open editors; only active one is visible */}
          {tabs.map((tab) => (
            <BlockEditor
              key={tab.rel_path}
              relPath={tab.rel_path}
              content={tab.content as unknown as string}
              isActive={tab.rel_path === activeRelPath}
              isPopoverOpen={popover !== null}
              onEdit={onEdit}
              onLinkClick={handleLinkClick}
              onOpenInNote={handleOpenInNote}
              onSelectionChange={tab.rel_path === activeRelPath ? handleSelectionChange : () => {}}
              onCommentClick={tab.rel_path === activeRelPath ? handleCommentBtnClick : () => {}}
              onCopyLinkClick={tab.rel_path === activeRelPath ? handleCopyLinkBtnClick : () => {}}
              scrollToBlockId={scrollTargets?.[tab.rel_path]}
              onScrollComplete={onScrollComplete ? () => onScrollComplete(tab.rel_path) : undefined}
            />
          ))}
        </div>
      ) : (
        <div className="notes-empty-state">
          <p>Open a file from the sidebar to start editing.</p>
          <button className="notes-btn" onClick={onNewTab}>Create new note</button>
        </div>
      )}

      {/* Comment input — floats above the selected text */}
      {selInfo && commentState !== 'hidden' && (
        <div
          className="notes-comment-input-float"
          style={{ top: selInfo.rect.top - 120, left: selInfo.rect.left + selInfo.rect.width / 2 }}
        >
          <textarea
            className="notes-comment-textarea"
            placeholder="Add a comment…"
            value={commentText}
            autoFocus
            onChange={(e) => setCommentText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleCommentSubmit();
              if (e.key === 'Escape') { setCommentState('hidden'); setSelInfo(null); }
            }}
            rows={3}
          />
          <div className="notes-comment-input-actions">
            <button
              className="notes-btn-ghost"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => { setCommentState('hidden'); setSelInfo(null); }}
            >Cancel</button>
            <button
              className="notes-btn"
              onMouseDown={(e) => e.preventDefault()}
              disabled={commentState === 'submitting' || !commentText.trim()}
              onClick={handleCommentSubmit}
            >{commentState === 'submitting' ? '…' : 'Save'}</button>
          </div>
        </div>
      )}

      {/* Doc preview popover — rendered outside the conditional so it layers on top */}
      {popover && (
        <DocPreviewPopover
          relPath={popover.relPath}
          blockId={popover.blockId}
          anchorEl={popover.anchorEl}
          onOpenInTab={onOpenInTab}
          onClose={() => setPopover(null)}
        />
      )}
    </div>
  );
}

