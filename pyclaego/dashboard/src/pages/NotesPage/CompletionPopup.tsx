/**
 * CompletionPopup.tsx — Autocomplete dropdown for #tag and @link input popups.
 *
 * Renders a positioned dropdown list of candidates fetched from the backend.
 * Supports keyboard navigation (↑↓), Enter to select, Escape handling by parent.
 */
import { useEffect, useRef, type RefObject } from 'react'
import type { AutocompleteCandidate } from './notesRpc'

interface Props {
  visible: boolean
  kind: 'tag' | 'link'
  candidates: AutocompleteCandidate[]
  highlightIndex: number
  onSelect: (candidate: AutocompleteCandidate) => void
  inputRef: RefObject<HTMLInputElement | null>
}

export default function CompletionPopup({
  visible,
  kind,
  candidates,
  highlightIndex,
  onSelect,
  inputRef,
}: Props) {
  const listRef = useRef<HTMLDivElement | null>(null)

  // Scroll highlighted item into view
  useEffect(() => {
    if (!listRef.current) return
    const item = listRef.current.querySelector('.notes-completion-item.highlight') as HTMLElement | null
    if (item) {
      item.scrollIntoView({ block: 'nearest' })
    }
  }, [highlightIndex])

  if (!visible || candidates.length === 0) return null

  return (
    <div
      className="notes-completion-dropdown"
      ref={listRef}
    >
      {candidates.map((c, i) => (
        <div
          key={c.value}
          className={`notes-completion-item${i === highlightIndex ? ' highlight' : ''}`}
          onMouseDown={(e) => {
            e.preventDefault() // prevent input blur before click
            onSelect(c)
          }}
          onMouseEnter={() => {
            // Highlight on hover is not needed here; parent manages highlightIndex
            // via onKeyDown. We keep this for optional mouse-driven UX enhancement.
          }}
        >
          <span className="notes-completion-icon">{kind === 'tag' ? '#' : '@'}</span>
          <span className="notes-completion-label">{c.label}</span>
          {kind === 'link' && (
            <span style={{ fontSize: 11, color: '#999', marginLeft: 'auto', flexShrink: 0 }}>
              {c.value}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
