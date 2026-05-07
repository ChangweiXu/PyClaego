/**
 * useDraggableSplit — generic draggable-splitter hook.
 *
 * Returns [ratio, splitterProps] where ratio is in 0.1–0.9 and
 * splitterProps can be spread onto a <div> handle element.
 *
 * @param axis   'v' = vertical split (left/right columns), cursor col-resize
 *               'h' = horizontal split (top/bottom rows), cursor row-resize
 * @param containerRef  ref to the container whose bounding rect defines 0–1
 * @param initial       starting ratio (default 0.5)
 */

import { useRef, useState } from 'react';
import type { RefObject, HTMLAttributes } from 'react';

export function useDraggableSplit(
  axis: 'v' | 'h',
  containerRef: RefObject<HTMLElement>,
  initial: number,
): [number, HTMLAttributes<HTMLDivElement>] {
  const [ratio, setRatio] = useState(initial);
  const dragging = useRef(false);

  const splitterProps: HTMLAttributes<HTMLDivElement> = {
    onMouseDown: (e) => {
      e.preventDefault();
      dragging.current = true;
      document.body.style.cursor = axis === 'v' ? 'col-resize' : 'row-resize';
      document.body.style.userSelect = 'none';

      const onMouseMove = (ev: MouseEvent) => {
        if (!dragging.current || !containerRef.current) return;
        const rect = containerRef.current.getBoundingClientRect();
        const r = axis === 'v'
          ? (ev.clientX - rect.left) / rect.width
          : (ev.clientY - rect.top) / rect.height;
        setRatio(Math.min(0.9, Math.max(0.1, r)));
      };

      const onMouseUp = () => {
        dragging.current = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    },
  };

  return [ratio, splitterProps];
}
