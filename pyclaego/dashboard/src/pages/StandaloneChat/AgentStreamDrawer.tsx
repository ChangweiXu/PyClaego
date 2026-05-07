/**
 * AgentStreamDrawer — right-side sliding drawer for viewing agent stream content.
 *
 * Subscribes to agentStreamStore for real-time updates, or shows
 * the complete markdown when stream is finished.
 * Includes backdrop for click-to-dismiss and Escape key support.
 */

import { useRef, useEffect, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useAgentStreamStore } from '../../store/agentStreams';

export interface AgentStreamDrawerProps {
  open: boolean;
  streamKey: string | null;
  onClose: () => void;
}

export function AgentStreamDrawer({ open, streamKey, onClose }: AgentStreamDrawerProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

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

  const stream = useAgentStreamStore((s) =>
    streamKey ? s.streams[streamKey] ?? null : null,
  );

  // Auto-scroll as chunks arrive
  useEffect(() => {
    if (scrollRef.current && stream?.chunks.length) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [stream?.chunks.length]);

  // Build display content from chunks
  const displayContent = useMemo(() => {
    if (!stream) return '';
    let text = '';
    let thinkingOpen = false;
    for (const c of stream.chunks) {
      if (c.chunk_type === 'thinking_delta') {
        if (!thinkingOpen) {
          text += '<details open><summary>💭 Thinking</summary>\n\n';
          thinkingOpen = true;
        }
        text += (c.content ?? '');
      } else if (c.chunk_type === 'text_delta') {
        if (thinkingOpen) {
          text += '\n\n</details>\n\n';
          thinkingOpen = false;
        }
        text += (c.content ?? '');
      } else if (c.chunk_type === 'round_separator') {
        if (thinkingOpen) {
          text += '\n\n</details>\n\n';
          thinkingOpen = false;
        }
        text += '\n\n' + (c.content ?? '') + '\n\n';
      } else if (c.chunk_type === 'tool_call_start') {
        if (thinkingOpen) {
          text += '\n\n</details>\n\n';
          thinkingOpen = false;
        }
        text += `\n\n🔧 **${c.tool_call_name ?? 'tool'}** `;
      } else if (c.chunk_type === 'tool_call_end') {
        text += '✅\n\n';
      } else if (c.chunk_type === 'fail') {
        if (thinkingOpen) {
          text += '\n\n</details>\n\n';
          thinkingOpen = false;
        }
        text += `\n\n> ⚠️ **Error**: ${c.error_message || c.content || 'Stream failed'}\n\n`;
      }
    }
    if (thinkingOpen) text += '\n\n</details>';
    return text;
  }, [stream]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div className="agent-stream-drawer-backdrop" onClick={onClose} />

      {/* Drawer panel */}
      <div className="agent-stream-drawer" role="dialog" aria-label="Agent Stream">
      <div className="agent-stream-drawer-header">
        <span>
          {stream?.subagentId
            ? `Subagent ${stream.subagentId.slice(-8)}`
            : 'Agent Stream'}
          {stream && !stream.finished && <span className="agent-stream-drawer-spinner" />}
          {stream?.finished && <span className="agent-stream-drawer-done"> ✓</span>}
        </span>
        <button className="agent-stream-drawer-close" onClick={onClose}>✕</button>
      </div>
      <div className="agent-stream-drawer-body markdown-body" ref={scrollRef}>
        {stream ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {displayContent}
          </ReactMarkdown>
        ) : (
          <div className="agent-stream-drawer-empty">
            Select an agent to view its stream.
          </div>
        )}
      </div>
      </div>
    </>
  );
}
