import type { ReactNode } from 'react';
import type { StackSchema } from '../schema/types';

interface StackProps {
  gap?: number;
  children: ReactNode;
}

export function Stack({ gap = 12, children }: StackProps) {
  return (
    <div className="p-stack" style={{ gap }}>
      {children}
    </div>
  );
}
