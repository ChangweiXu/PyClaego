/**
 * CustomLinkToolbar.tsx
 *
 * Replaces BlockNote's default link toolbar for internal bdx note links.
 *
 * Behaviour:
 *   - Hover: shows the target note filename + section anchor, with buttons
 *     "↗ Open" (opens note in tab), "✎ Edit" (inline edit form), "✕" (delete link).
 *   - "↗ Open": fires onOpenInNote(href, buttonEl) which opens the note in an
 *     editor tab (no blank browser tab).
 *   - "✎ Edit": expands an inline form with two fields — Display text + Target URL.
 *     Saving calls BlockNote's editLink(url, text).
 */
import { useState, useRef, useCallback } from 'react';
import type { LinkToolbarProps } from '@blocknote/react';

export interface CustomLinkToolbarProps extends LinkToolbarProps {
  /** Called when the user clicks "↗ Open". Receives the raw href and the button element. */
  onOpenInNote: (href: string, anchorEl: HTMLElement) => void;
}

/** Parses a bdx href like "path/to/file.bdx#b_xyz" into { filename, section }. */
function parseBdxHref(href: string): { filename: string; section: string } {
  const hashIdx = href.lastIndexOf('#');
  const path = hashIdx > 0 ? href.slice(0, hashIdx) : href;
  const section = hashIdx > 0 ? href.slice(hashIdx + 1) : '';
  const filename = path.split('/').pop() ?? path;
  return { filename, section };
}

export default function CustomLinkToolbar({
  url,
  text,
  editLink,
  deleteLink,
  startHideTimer,
  stopHideTimer,
  onOpenInNote,
}: CustomLinkToolbarProps) {
  const [editing, setEditing] = useState(false);
  const [editUrl, setEditUrl] = useState(url);
  const [editText, setEditText] = useState(text);
  const openBtnRef = useRef<HTMLButtonElement>(null);

  const { filename, section } = parseBdxHref(url);

  const handleSaveEdit = useCallback(() => {
    editLink(editUrl, editText);
    setEditing(false);
  }, [editLink, editUrl, editText]);

  const handleOpenInNote = useCallback(() => {
    if (openBtnRef.current) {
      onOpenInNote(url, openBtnRef.current);
    }
    startHideTimer();
  }, [onOpenInNote, url, startHideTimer]);

  const handleStartEditing = useCallback(() => {
    setEditUrl(url);
    setEditText(text);
    setEditing(true);
    stopHideTimer();
  }, [url, text, stopHideTimer]);

  return (
    <div
      className="custom-link-toolbar"
      onMouseEnter={stopHideTimer}
      onMouseLeave={startHideTimer}
    >
      {!editing ? (
        <>
          {/* Target info */}
          <span className="custom-link-toolbar-target" title={url}>
            {filename}
            {section && (
              <span className="custom-link-toolbar-section">#{section}</span>
            )}
          </span>

          {/* Action buttons */}
          <button
            ref={openBtnRef}
            className="custom-link-toolbar-btn"
            title="Open in tab"
            onClick={handleOpenInNote}
          >
            ↗ Open
          </button>
          <button
            className="custom-link-toolbar-btn"
            title="Edit link"
            onClick={handleStartEditing}
          >
            ✎ Edit
          </button>
          <button
            className="custom-link-toolbar-btn custom-link-toolbar-btn-danger"
            title="Remove link"
            onClick={deleteLink}
          >
            ✕
          </button>
        </>
      ) : (
        /* Inline edit form */
        <div
          className="custom-link-toolbar-edit"
          onMouseEnter={stopHideTimer}
        >
          <div className="custom-link-toolbar-edit-row">
            <label className="custom-link-toolbar-label">Text</label>
            <input
              className="custom-link-toolbar-input"
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSaveEdit();
                if (e.key === 'Escape') setEditing(false);
              }}
              autoFocus
            />
          </div>
          <div className="custom-link-toolbar-edit-row">
            <label className="custom-link-toolbar-label">Target</label>
            <input
              className="custom-link-toolbar-input"
              value={editUrl}
              onChange={(e) => setEditUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSaveEdit();
                if (e.key === 'Escape') setEditing(false);
              }}
            />
          </div>
          <div className="custom-link-toolbar-edit-actions">
            <button
              className="custom-link-toolbar-btn"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
            <button
              className="custom-link-toolbar-btn custom-link-toolbar-btn-primary"
              onClick={handleSaveEdit}
            >
              Save
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
