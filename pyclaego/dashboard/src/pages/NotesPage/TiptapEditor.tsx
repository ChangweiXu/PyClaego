/**
 * TiptapEditor.tsx — Core Tiptap editor component for Notes
 *
 * Replaces the BlockNote-based editor with a Tiptap-based implementation.
 * Handles:
 * - Rich text editing (WYSIWYG)
 * - #tag insertion and rendering
 * - @document link insertion and rendering
 * - Auto-save with debounce
 * - Comment selection functionality
 */
import { useEffect, useRef, useCallback, useState } from 'react'
import { useEditor, EditorContent, type Editor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Link from '@tiptap/extension-link'
import Image from '@tiptap/extension-image'
import Underline from '@tiptap/extension-underline'
import Highlight from '@tiptap/extension-highlight'
import TextAlign from '@tiptap/extension-text-align'
import TextStyle from '@tiptap/extension-text-style'
import Color from '@tiptap/extension-color'
import Subscript from '@tiptap/extension-subscript'
import Superscript from '@tiptap/extension-superscript'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import { TagExtension } from './extensions/TagExtension'
import { MentionExtension } from './extensions/MentionExtension'
import CompletionPopup from './CompletionPopup'
import { useNotesRpc } from './NotesRpcContext'
import type { NoteRpcClient, AutocompleteCandidate } from './notesRpc'
import type { OpenTab, SaveState } from './notesStore'

// Debounce delay for auto-save (ms)
const AUTOSAVE_DELAY = 1500

interface Props {
  tabs: OpenTab[]
  activeRelPath: string | null
  psId: string
  widgetId: string
  onTabClick: (relPath: string) => void
  onTabClose: (relPath: string) => void
  onEdit: (relPath: string, content: Record<string, unknown>) => void
  onNewTab: () => void
  onReloadTab: (relPath: string) => void
  onOpenInTab: (relPath: string, blockId?: string) => void
  onCommentCreated?: () => void
  scrollTargets?: Record<string, string>
  onScrollComplete?: (relPath: string) => void
  rpcClient: NoteRpcClient
}

function saveStateLabel(state: SaveState): { text: string; cls: string } {
  switch (state) {
    case 'dirty': return { text: '● Unsaved', cls: 'dirty' }
    case 'saving': return { text: '⟳ Saving…', cls: 'saving' }
    case 'error': return { text: '✗ Error', cls: 'error' }
    case 'externally_modified': return { text: '⚠ Changed externally', cls: 'warn' }
    case 'deleted': return { text: '✗ Deleted', cls: 'error' }
    default: return { text: 'Saved', cls: 'clean' }
  }
}

/** Inner editor component per open tab. Kept alive while the tab is open. */
function TiptapEditorInstance({
  relPath,
  content,
  isActive,
  onEdit,
  onLinkClick,
  onOpenInNote,
  onSelectionChange,
  onCommentClick,
  onCopyLinkClick,
  scrollToBlockId,
  onScrollComplete,
}: {
  relPath: string
  content: Record<string, unknown>
  isActive: boolean
  onEdit: (relPath: string, jsonContent: Record<string, unknown>) => void
  onLinkClick: (href: string, anchorEl: HTMLElement) => void
  onOpenInNote: (href: string, anchorEl: HTMLElement) => void
  onSelectionChange: (blockId: string | null, rect: DOMRect | null) => void
  onCommentClick: () => void
  onCopyLinkClick: () => void
  scrollToBlockId?: string
  onScrollComplete?: () => void
}) {
  const rpcClient = useNotesRpc()
  const [tagPopup, setTagPopup] = useState<{
    visible: boolean
    input: string
    candidates: AutocompleteCandidate[]
    highlightIndex: number
    loading: boolean
  }>({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
  const [mentionPopup, setMentionPopup] = useState<{
    visible: boolean
    input: string
    candidates: AutocompleteCandidate[]
    highlightIndex: number
    loading: boolean
  }>({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
  const [mentionResolving, setMentionResolving] = useState(false)
  const tagInputRef = useRef<HTMLInputElement | null>(null)
  const mentionInputRef = useRef<HTMLInputElement | null>(null)

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
      }),
      Placeholder.configure({
        placeholder: 'Start writing or press # for a tag, @ to link a document...',
      }),
      Link.configure({
        openOnClick: false,
        linkOnPaste: true,
      }),
      Image.configure({
        inline: true,
        allowBase64: true,
      }),
      Underline,
      Highlight.configure({ multicolor: true }),
      TextStyle,
      Color,
      Subscript,
      Superscript,
      TextAlign.configure({
        types: ['heading', 'paragraph'],
      }),
      TaskList,
      TaskItem.configure({
        nested: true,
      }),
      Table.configure({
        resizable: true,
      }),
      TableRow,
      TableCell,
      TableHeader,
      TagExtension,
      MentionExtension.configure({
        onClick: (mentionText: string) => {
          // Parse and open the mentioned document
          const parts = mentionText.split('#')
          const relPath = parts[0].endsWith('.bdx') ? parts[0] : `${parts[0]}.bdx`
          const blockId = parts[1] ?? ''
          const href = blockId ? `${relPath}#${blockId}` : relPath
          onOpenInNote(href, document.createElement('span'))
        },
      }),
    ],
    content: content,
    editorProps: {
      attributes: {
        class: 'tiptap-editor-content',
      },
      handleDOMEvents: {
        click: (view, event) => {
          const target = event.target as HTMLElement
          const anchor = target.closest('a')
          if (anchor) {
            event.preventDefault()
            const href = anchor.getAttribute('href') ?? ''
            if (href) {
              onLinkClick(href, anchor)
            }
            return true
          }
          return false
        },
      },
    },
    onUpdate: ({ editor }) => {
      // Debounce — don't dispatch to parent on every keystroke.
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => {
        handleSave(editor!)
      }, AUTOSAVE_DELAY)
    },
  })

  // Stable refs for callbacks
  const onEditRef = useRef(onEdit)
  onEditRef.current = onEdit

  // Core save: send Tiptap JSON directly to parent (backend handles XML conversion)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // JSON deep equality helper
  const jsonEq = (a: Record<string, unknown>, b: Record<string, unknown>) =>
    JSON.stringify(a) === JSON.stringify(b)

  const prevContentRef = useRef(content)

  const handleSave = useCallback((ed: typeof editor) => {
    if (!ed) return
    const json = ed.getJSON()
    // Mark that WE initiated this change so the useEffect below doesn't
    // call setContent when the same JSON comes back via the parent state.
    prevContentRef.current = json
    onEditRef.current(relPath, json)
  }, [relPath])

  // When the tab content changes externally, re-sync editor state
  useEffect(() => {
    if (!jsonEq(content, prevContentRef.current) && editor) {
      prevContentRef.current = content
      editor.commands.setContent(content)
    }
  }, [content, editor])

  // Tag insert handler
  const handleTagInsert = useCallback(() => {
    const raw = tagPopup.input.trim().replace(/^#+/, '')
    setTagPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
    if (!raw || !editor) { editor?.chain().focus().run(); return }
    editor.chain().focus().insertContent({
      type: 'text',
      text: `#${raw}`,
      marks: [{ type: 'tag', attrs: { name: raw } }],
    }).run()
  }, [editor, tagPopup.input])

  // Mention insert handler — calls backend resolve_link for path validation & doc_id lookup
  const handleMentionInsert = useCallback(async () => {
    const raw = mentionPopup.input.trim().replace(/^@+/, '')
    if (!raw || !editor) {
      setMentionPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
      editor?.chain().focus().run()
      return
    }
    // Parse raw target and optional blockId from user input
    const hashIdx = raw.indexOf('#')
    const rawTarget = hashIdx >= 0 ? raw.slice(0, hashIdx) : raw
    const blockId = hashIdx >= 0 ? raw.slice(hashIdx + 1) : ''

    setMentionResolving(true)
    try {
      // Call backend to resolve the path (handles / prefix, .bdx suffix, relative dirs)
      const resolved = await rpcClient.resolveLink(relPath, rawTarget, blockId)
      const href = resolved.exists && resolved.doc_id
        ? (blockId ? `${resolved.doc_id}#${blockId}` : resolved.doc_id)
        : ''
      const displayText = resolved.rel_path.split('/').pop()?.replace(/\.bdx$/, '') ?? rawTarget

      setMentionPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
      editor.chain().focus().insertContent({
        type: 'text',
        text: `@${displayText}`,
        marks: [{ type: 'link', attrs: { href } }],
      }).run()
    } catch (e) {
      console.error('[mention] resolve_link failed', e)
      setMentionPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
      editor.chain().focus().run()
    } finally {
      setMentionResolving(false)
    }
  }, [editor, mentionPopup.input, relPath, rpcClient])

  // Autocomplete fetch for tag input — debounce 150ms
  useEffect(() => {
    if (!tagPopup.visible) return
    if (tagPopup.input.length === 0) {
      setTagPopup((p) => ({ ...p, candidates: [], highlightIndex: -1 }))
      return
    }
    const timer = setTimeout(async () => {
      setTagPopup((p) => ({ ...p, loading: true }))
      try {
        const res = await rpcClient.autocomplete(tagPopup.input, 'tag')
        setTagPopup((p) => ({ ...p, candidates: res.candidates, highlightIndex: 0, loading: false }))
      } catch {
        setTagPopup((p) => ({ ...p, candidates: [], loading: false }))
      }
    }, 150)
    return () => clearTimeout(timer)
  }, [tagPopup.visible, tagPopup.input, rpcClient])

  // Autocomplete fetch for mention input — debounce 150ms
  useEffect(() => {
    if (!mentionPopup.visible) return
    if (mentionPopup.input.length === 0) {
      setMentionPopup((p) => ({ ...p, candidates: [], highlightIndex: -1 }))
      return
    }
    const timer = setTimeout(async () => {
      setMentionPopup((p) => ({ ...p, loading: true }))
      try {
        const res = await rpcClient.autocomplete(mentionPopup.input, 'link')
        setMentionPopup((p) => ({ ...p, candidates: res.candidates, highlightIndex: 0, loading: false }))
      } catch {
        setMentionPopup((p) => ({ ...p, candidates: [], loading: false }))
      }
    }, 150)
    return () => clearTimeout(timer)
  }, [mentionPopup.visible, mentionPopup.input, rpcClient])

  // Keyboard shortcuts
  useEffect(() => {
    if (!isActive || !editor) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
        handleSave(editor)
        return
      }
      if (e.key === '#' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const target = e.target as HTMLElement
        if (target.tagName !== 'INPUT' && target.tagName !== 'TEXTAREA') {
          e.preventDefault()
          setTagPopup({ visible: true, input: '', candidates: [], highlightIndex: -1, loading: false })
        }
      }
      if (e.key === '@' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const target = e.target as HTMLElement
        if (target.tagName !== 'INPUT' && target.tagName !== 'TEXTAREA') {
          e.preventDefault()
          setMentionPopup({ visible: true, input: '', candidates: [], highlightIndex: -1, loading: false })
        }
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [isActive, editor, handleSave])

  // Track text selection and notify parent with block ID + rect
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!isActive || !editor) return
    const handler = () => {
      const sel = window.getSelection()
      if (!sel || sel.isCollapsed || !sel.rangeCount) {
        onSelectionChange(null, null)
        return
      }
      const range = sel.getRangeAt(0)
      const el = wrapperRef.current
      if (!el || !el.contains(range.commonAncestorContainer)) {
        onSelectionChange(null, null)
        return
      }
      const rect = range.getBoundingClientRect()
      // Get current block ID from Tiptap
      try {
        const { from } = editor.state.selection
        const resolved = editor.state.doc.resolve(from)
        const blockId = resolved.parent.attrs?.id as string | null
        onSelectionChange(blockId ?? null, rect)
      } catch {
        onSelectionChange(null, null)
      }
    }
    document.addEventListener('selectionchange', handler)
    return () => document.removeEventListener('selectionchange', handler)
  }, [isActive, editor, onSelectionChange])

  // Scroll to block once the editor is active
  useEffect(() => {
    if (!isActive || !scrollToBlockId || !wrapperRef.current) return
    let attempts = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    const tryScroll = () => {
      const el = wrapperRef.current?.querySelector(`[data-id="${scrollToBlockId}"]`)
      if (el) {
        el.scrollIntoView({ block: 'start', behavior: 'smooth' })
        onScrollComplete?.()
      } else if (attempts < 15) {
        attempts++
        timer = setTimeout(tryScroll, 80)
      }
    }
    requestAnimationFrame(tryScroll)
    return () => { if (timer) clearTimeout(timer) }
  }, [isActive, scrollToBlockId, onScrollComplete])

  if (!editor) return null

  return (
    <div
      ref={wrapperRef}
      className="tiptap-editor-wrapper"
      style={{ display: isActive ? 'flex' : 'none', flexDirection: 'column', flex: 1, overflow: 'auto' }}
    >
      {/* Formatting toolbar */}
      <div className="notes-editor-topbar">
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Paragraph"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().setParagraph().run() }}
          >¶</button>
          <button
            className="notes-topbar-btn"
            title="Heading 1"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleHeading({ level: 1 }).run() }}
          >H1</button>
          <button
            className="notes-topbar-btn"
            title="Heading 2"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleHeading({ level: 2 }).run() }}
          >H2</button>
          <button
            className="notes-topbar-btn"
            title="Heading 3"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleHeading({ level: 3 }).run() }}
          >H3</button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Bold"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleBold().run() }}
          ><b>B</b></button>
          <button
            className="notes-topbar-btn"
            title="Italic"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleItalic().run() }}
          ><i>I</i></button>
          <button
            className="notes-topbar-btn"
            title="Underline"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleUnderline().run() }}
          ><u>U</u></button>
          <button
            className="notes-topbar-btn"
            title="Strikethrough"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleStrike().run() }}
          ><s>S</s></button>
          <button
            className="notes-topbar-btn"
            title="Inline code"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleCode().run() }}
          >&lt;/&gt;</button>
          <button
            className="notes-topbar-btn"
            title="Highlight"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleHighlight().run() }}
          ><mark>H</mark></button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Subscript"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleSubscript().run() }}
          >X<sub>2</sub></button>
          <button
            className="notes-topbar-btn"
            title="Superscript"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleSuperscript().run() }}
          >X<sup>2</sup></button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Align left"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().setTextAlign('left').run() }}
          >⇤</button>
          <button
            className="notes-topbar-btn"
            title="Align center"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().setTextAlign('center').run() }}
          >⇔</button>
          <button
            className="notes-topbar-btn"
            title="Align right"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().setTextAlign('right').run() }}
          >⇥</button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Bullet list"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleBulletList().run() }}
          >• List</button>
          <button
            className="notes-topbar-btn"
            title="Numbered list"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleOrderedList().run() }}
          >1. List</button>
          <button
            className="notes-topbar-btn"
            title="Task list"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleTaskList().run() }}
          >☑ Task</button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Blockquote"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleBlockquote().run() }}
          >&ldquo; Quote</button>
          <button
            className="notes-topbar-btn"
            title="Code block"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().toggleCodeBlock().run() }}
          >&lt;Code&gt;</button>
          <button
            className="notes-topbar-btn"
            title="Horizontal rule"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().setHorizontalRule().run() }}
          >—</button>
        </div>
        <div className="notes-topbar-sep" />
        <div className="notes-topbar-group">
          <button
            className="notes-topbar-btn"
            title="Insert table"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run() }}
          >⊞ Table</button>
          <button
            className={`notes-topbar-btn${tagPopup.visible ? ' active' : ''}`}
            title="Insert tag (#tag)"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              if (tagPopup.visible) {
                setTagPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
                editor.chain().focus().run()
              } else {
                setTagPopup({ visible: true, input: '', candidates: [], highlightIndex: -1, loading: false })
              }
            }}
          >#</button>
          <button
            className={`notes-topbar-btn${mentionPopup.visible ? ' active' : ''}`}
            title="Insert document link (@doc)"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              if (mentionPopup.visible) {
                setMentionPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
                editor.chain().focus().run()
              } else {
                setMentionPopup({ visible: true, input: '', candidates: [], highlightIndex: -1, loading: false })
              }
            }}
          >@</button>
        </div>
      </div>

      {/* Tag insert popup */}
      {tagPopup.visible && (
        <div className="notes-tag-insert-popup" style={{ position: 'relative', top: 0 }}>
          <span className="notes-tag-insert-hash">#</span>
          <input
            ref={tagInputRef}
            autoFocus
            className="notes-tag-insert-input"
            placeholder="tag name…"
            value={tagPopup.input}
            onChange={(e) => setTagPopup((p) => ({ ...p, input: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setTagPopup((p) => ({
                  ...p,
                  highlightIndex: Math.min(p.candidates.length - 1, p.highlightIndex + 1),
                }))
                return
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault()
                setTagPopup((p) => ({
                  ...p,
                  highlightIndex: Math.max(0, p.highlightIndex - 1),
                }))
                return
              }
              if (e.key === 'Enter') {
                e.preventDefault()
                // If a candidate is highlighted, select it (fill the input); otherwise insert
                const idx = tagPopup.highlightIndex
                if (idx >= 0 && idx < tagPopup.candidates.length) {
                  const selected = tagPopup.candidates[idx]
                  setTagPopup((p) => ({ ...p, input: selected.value, candidates: [], highlightIndex: -1 }))
                } else {
                  handleTagInsert()
                }
                return
              }
              if (e.key === 'Escape') {
                e.preventDefault()
                if (tagPopup.candidates.length > 0) {
                  setTagPopup((p) => ({ ...p, candidates: [], highlightIndex: -1 }))
                } else {
                  setTagPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
                  editor.chain().focus().run()
                }
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
            onClick={() => { setTagPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false }); editor.chain().focus().run() }}
          >✕</button>
          <CompletionPopup
            visible={tagPopup.candidates.length > 0}
            kind="tag"
            candidates={tagPopup.candidates}
            highlightIndex={tagPopup.highlightIndex}
            onSelect={(c) => setTagPopup((p) => ({ ...p, input: c.value, candidates: [], highlightIndex: -1 }))}
            inputRef={tagInputRef}
          />
        </div>
      )}

      {/* Mention insert popup */}
      {mentionPopup.visible && (
        <div className="notes-tag-insert-popup" style={{ position: 'relative', top: 0 }}>
          <span className="notes-tag-insert-hash">@</span>
          <input
            ref={mentionInputRef}
            autoFocus
            className="notes-tag-insert-input"
            placeholder="document path#blockId…"
            value={mentionPopup.input}
            onChange={(e) => setMentionPopup((p) => ({ ...p, input: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setMentionPopup((p) => ({
                  ...p,
                  highlightIndex: Math.min(p.candidates.length - 1, p.highlightIndex + 1),
                }))
                return
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault()
                setMentionPopup((p) => ({
                  ...p,
                  highlightIndex: Math.max(0, p.highlightIndex - 1),
                }))
                return
              }
              if (e.key === 'Enter') {
                e.preventDefault()
                const idx = mentionPopup.highlightIndex
                if (idx >= 0 && idx < mentionPopup.candidates.length) {
                  const selected = mentionPopup.candidates[idx]
                  setMentionPopup((p) => ({ ...p, input: selected.value, candidates: [], highlightIndex: -1 }))
                } else {
                  handleMentionInsert()
                }
                return
              }
              if (e.key === 'Escape') {
                e.preventDefault()
                if (mentionPopup.candidates.length > 0) {
                  setMentionPopup((p) => ({ ...p, candidates: [], highlightIndex: -1 }))
                } else {
                  setMentionPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false })
                  editor.chain().focus().run()
                }
              }
            }}
          />
          <button
            className="notes-topbar-btn"
            disabled={mentionResolving}
            onMouseDown={(e) => e.preventDefault()}
            onClick={handleMentionInsert}
          >{mentionResolving ? 'Resolving…' : 'Insert'}</button>
          <button
            className="notes-topbar-btn"
            disabled={mentionResolving}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { setMentionPopup({ visible: false, input: '', candidates: [], highlightIndex: -1, loading: false }); editor.chain().focus().run() }}
          >✕</button>
          <CompletionPopup
            visible={mentionPopup.candidates.length > 0}
            kind="link"
            candidates={mentionPopup.candidates}
            highlightIndex={mentionPopup.highlightIndex}
            onSelect={(c) => setMentionPopup((p) => ({ ...p, input: c.value, candidates: [], highlightIndex: -1 }))}
            inputRef={mentionInputRef}
          />
        </div>
      )}

      {/* Tiptap editor content */}
      <EditorContent editor={editor} />
    </div>
  )
}

/** Main EditorPane component with tab management and comment functionality */
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
  rpcClient,
}: Props) {
  const activeTab = tabs.find((t) => t.rel_path === activeRelPath)

  // Selection toolbar state
  const [selInfo, setSelInfo] = useState<{ blockId: string; rect: DOMRect } | null>(null)
  // Comment input state
  const [commentState, setCommentState] = useState<'hidden' | 'input' | 'submitting'>('hidden')
  const [commentText, setCommentText] = useState('')

  const handleSelectionChange = useCallback((blockId: string | null, rect: DOMRect | null) => {
    if (!blockId || !rect) { setSelInfo(null); return }
    setSelInfo({ blockId, rect })
  }, [])

  const handleCopyBlockLink = useCallback(() => {
    if (!selInfo || !activeRelPath) return
    const link = `${activeRelPath}#${selInfo.blockId}`
    navigator.clipboard.writeText(link).catch(() => {})
    setSelInfo(null)
  }, [selInfo, activeRelPath])

  const handleCommentBtnClick = useCallback(() => {
    setCommentState('input')
  }, [])

  const handleCopyLinkBtnClick = useCallback(() => {
    handleCopyBlockLink()
  }, [handleCopyBlockLink])

  const handleCommentSubmit = useCallback(async () => {
    if (!selInfo || !activeRelPath || !commentText.trim()) return
    setCommentState('submitting')
    try {
      await rpcClient.createComment(activeRelPath, selInfo.blockId, commentText.trim())
      onCommentCreated?.()
    } catch (e) {
      console.error('Failed to create comment', e)
    } finally {
      setCommentText('')
      setCommentState('hidden')
      setSelInfo(null)
    }
  }, [selInfo, activeRelPath, commentText, psId, widgetId, onCommentCreated])

  // Link click handler — href is already normalized by backend/parseInlineContent
  const handleLinkClick = useCallback((href: string, anchorEl: HTMLElement) => {
    const hashIdx = href.lastIndexOf('#')
    const target = hashIdx > 0 ? href.slice(0, hashIdx) : href
    const blockId = hashIdx > 0 ? href.slice(hashIdx + 1) : ''
    if (!target) return  // Invalid link, do nothing
    onOpenInTab(target, blockId || undefined)
  }, [onOpenInTab])

  const handleOpenInNote = useCallback((href: string, _anchorEl: HTMLElement) => {
    const hashIdx = href.lastIndexOf('#')
    const target = hashIdx > 0 ? href.slice(0, hashIdx) : href
    const blockId = hashIdx > 0 ? href.slice(hashIdx + 1) : ''
    if (!target) return  // Invalid link, do nothing
    onOpenInTab(target, blockId || undefined)
  }, [onOpenInTab])

  return (
    <div className="notes-editor-pane">
      {/* Tab bar */}
      <div className="notes-tabbar">
        {tabs.map((tab) => {
          const { text, cls } = saveStateLabel(tab.saveState)
          const isActive = tab.rel_path === activeRelPath
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
                onClick={(e) => { e.stopPropagation(); onTabClose(tab.rel_path) }}
                title="Close tab"
              >×</button>
            </div>
          )
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
            <TiptapEditorInstance
              key={tab.rel_path}
              relPath={tab.rel_path}
              content={tab.content}
              isActive={tab.rel_path === activeRelPath}
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
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleCommentSubmit()
              if (e.key === 'Escape') { setCommentState('hidden'); setSelInfo(null) }
            }}
            rows={3}
          />
          <div className="notes-comment-input-actions">
            <button
              className="notes-btn-ghost"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => { setCommentState('hidden'); setSelInfo(null) }}
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
    </div>
  )
}
