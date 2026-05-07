import type { ReactNode } from 'react';

interface SplitProps {
  orientation?: 'h' | 'v';
  ratio?: number;
  left: ReactNode;
  right: ReactNode;
}

export function Split({ orientation = 'h', ratio = 0.6, left, right }: SplitProps) {
  const isH = orientation === 'h';
  const leftPct = `${ratio * 100}%`;
  const rightPct = `${(1 - ratio) * 100}%`;

  return (
    <div
      className={`p-split p-split-${isH ? 'h' : 'v'}`}
      style={
        isH
          ? { '--left-w': leftPct, '--right-w': rightPct } as React.CSSProperties
          : { '--top-h': leftPct, '--bottom-h': rightPct } as React.CSSProperties
      }
    >
      <div className="p-split-left">{left}</div>
      <div className="p-split-divider" />
      <div className="p-split-right">{right}</div>
    </div>
  );
}

// React is needed for CSSProperties
import React from 'react';
