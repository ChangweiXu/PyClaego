/**
 * StreamButton — round status button for each Agent row.
 *
 * States:
 *   - active (finished=false) → orange pulsing ring
 *   - done   (finished=true)  → green static circle with ✓
 *   - error  (has error chunk) → red tint
 */

import { useMemo } from 'react';
import type { AgentStreamState } from '../store/agentStreams';

export interface StreamButtonProps {
  stream: AgentStreamState | null;
  onClick: () => void;
  active?: boolean;
}

export function StreamButton({ stream, onClick, active }: StreamButtonProps) {
  const status = useMemo(() => {
    if (!stream) return 'idle';
    if (stream.finished) {
      const hasError = stream.chunks.some((c) => c.chunk_type === 'fail');
      return hasError ? 'error' : 'done';
    }
    return 'active';
  }, [stream]);

  const statusClass = `stream-btn stream-btn--${status}${active ? ' stream-btn--selected' : ''}`;

  const title = !stream
    ? 'No stream'
    : stream.finished
      ? 'Stream complete — click to review'
      : 'Streaming… — click to watch';

  return (
    <button className={statusClass} onClick={onClick} title={title}>
      {status === 'active' && <span className="stream-btn-pulse" />}
      {status === 'done' && <span className="stream-btn-done">✓</span>}
      {status === 'error' && <span className="stream-btn-error">✕</span>}
      {status === 'idle' && <span className="stream-btn-idle">—</span>}
    </button>
  );
}
