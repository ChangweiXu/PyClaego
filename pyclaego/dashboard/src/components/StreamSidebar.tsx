/**
 * StreamSidebar — full-content drawer panel.
 *
 * Slides in from the right when user clicks "Expand" on StreamBox.
 * Shows the complete assistant response with full markdown rendering,
 * including thinking content and all rounds.
 */

import { useEffect, useRef } from 'react';
import type { ChatMessage } from '../store/live';

export interface StreamSidebarProps {
  open: boolean;
  message: ChatMessage | null;
  onClose: () => void;
}

export function StreamSidebar({ open, message, onClose }: StreamSidebarProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Scroll to top when opening
  useEffect(() => {
    if (open && scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [open]);

  // Close on Escape
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    if (open) {
      document.addEventListener('keydown', onKey);
      return () => document.removeEventListener('keydown', onKey);
    }
  }, [open, onClose]);

  if (!open || !message) return null;

  const fullText = message.text || '';

  return (
    <>
      {/* Backdrop */}
      <div className="stream-sidebar-backdrop" onClick={onClose} />

      {/* Drawer panel */}
      <div className="stream-sidebar">
        <div className="stream-sidebar-header">
          <span className="stream-sidebar-title">
            Full Response
            {message.currentRound ? ` · Round ${message.currentRound}` : ''}
          </span>
          <div className="stream-sidebar-actions">
            <button
              className="stream-sidebar-btn"
              onClick={() => {
                navigator.clipboard.writeText(fullText).catch(() => {});
              }}
              title="Copy full response"
            >
              📋 Copy
            </button>
            <button
              className="stream-sidebar-btn"
              onClick={() => {
                if (scrollRef.current) {
                  scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
                }
              }}
              title="Jump to bottom"
            >
              ⬇ Bottom
            </button>
            <button
              className="stream-sidebar-btn stream-sidebar-close"
              onClick={onClose}
            >
              ✕
            </button>
          </div>
        </div>

        <div className="stream-sidebar-content" ref={scrollRef}>
          <div
            className="stream-sidebar-text markdown-body"
            dangerouslySetInnerHTML={{ __html: renderFullMarkdown(fullText) }}
          />
        </div>
      </div>
    </>
  );
}

/** Full markdown render (same as StreamBox but for final content) */
function renderFullMarkdown(text: string): string {
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="language-$1">$2</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/^---$/gm, '<hr>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>');

  html = '<p>' + html + '</p>';

  html = html.replace(/&lt;details&gt;/g, '<details>');
  html = html.replace(/&lt;\/details&gt;/g, '</details>');
  html = html.replace(/&lt;summary&gt;/g, '<summary>');
  html = html.replace(/&lt;\/summary&gt;/g, '</summary>');

  return html;
}
