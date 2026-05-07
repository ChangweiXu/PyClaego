/**
 * StreamBox — real-time streaming content overlay.
 *
 * Shows streaming assistant response in a centered overlay above the chat log.
 * Features:
 *   - max-height with auto-scroll
 *   - thinking content collapsed in <details>
 *   - round separators
 *   - tool call badges
 *   - blinking cursor while streaming
 *   - Expand button → opens StreamSidebar
 *   - fades out when stream ends (content moves to message list)
 */

import { useEffect, useRef, useState } from 'react';
import type { ChatMessage, ToolCallInfo } from '../store/live';

export interface StreamBoxProps {
  message: ChatMessage;
  onExpand: () => void;
  onFadeComplete?: () => void;
}

/** Simple copy button (inline re-export) */
function ChatCopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <button className="chat-copy-btn" onClick={handleCopy} title="Copy to clipboard">
      {copied ? '✓' : '⎘'}
    </button>
  );
}

export function StreamBox({ message, onExpand, onFadeComplete }: StreamBoxProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [fading, setFading] = useState(false);

  // Auto-scroll to bottom as content streams
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [message.text]);

  // Fade out when streaming ends
  useEffect(() => {
    if (!message.streaming) {
      setFading(true);
      const timer = setTimeout(() => onFadeComplete?.(), 600);
      return () => clearTimeout(timer);
    }
    setFading(false);
  }, [message.streaming, onFadeComplete]);

  return (
    <div className={`stream-box ${fading ? 'stream-box-fading' : ''}`}>
      {/* Header */}
      <div className="stream-box-header">
        <span className="stream-box-status">
          {message.streaming ? '🔄 Streaming…' : '✅ Complete'}
          {message.currentRound ? ` · Round ${message.currentRound}` : ''}
        </span>
        <button
          className="stream-box-expand-btn"
          onClick={onExpand}
          title="Expand to full view"
        >
          📋 Expand
        </button>
      </div>

      {/* Content */}
      <div className="stream-box-content" ref={scrollRef}>
        <div
          className="stream-box-text markdown-body"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(message.text) }}
        />
        {message.streaming && <span className="stream-box-cursor" />}
      </div>

      {/* Tool calls */}
      {message.toolCalls && message.toolCalls.length > 0 && (
        <div className="stream-box-tools">
          {message.toolCalls.map((tc: ToolCallInfo) => (
            <span
              key={tc.id}
              className={`chat-tool-call ${tc.status}`}
              title={tc.status === 'running' ? `Using tool: ${tc.name}…` : `Completed: ${tc.name}`}
            >
              {tc.status === 'running' ? '⏳' : '✅'} {tc.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Basic markdown-to-HTML renderer (handles details/summary, code blocks, bold, etc.) */
function renderMarkdown(text: string): string {
  let html = text
    // Escape HTML
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    // Code blocks (```)
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="language-$1">$2</code></pre>')
    // Inline code (`)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // Bold (**)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    // Italic (*)
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    // Horizontal rule (---)
    .replace(/^---$/gm, '<hr>')
    // Line breaks
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>');

  // Wrap in paragraphs
  html = '<p>' + html + '</p>';

  // Preserve <details>/<summary> (already HTML from backend)
  html = html.replace(/&lt;details&gt;/g, '<details>');
  html = html.replace(/&lt;\/details&gt;/g, '</details>');
  html = html.replace(/&lt;summary&gt;/g, '<summary>');
  html = html.replace(/&lt;\/summary&gt;/g, '</summary>');

  return html;
}
