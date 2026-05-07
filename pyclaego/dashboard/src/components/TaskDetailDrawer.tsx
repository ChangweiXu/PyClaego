/**
 * TaskDetailDrawer — slide-in drawer for viewing full task details.
 *
 * Opens from the right when a task's detail button is clicked in ChatRenderer.
 * Wraps the existing TaskDetailPane component with backdrop + Escape close.
 */

import { useEffect } from 'react';
import type { TaskNode } from '../api';
import TaskDetailPane from './TaskDetailPane';

export interface TaskDetailDrawerProps {
  open: boolean;
  task: TaskNode | null;
  onClose: () => void;
}

export function TaskDetailDrawer({ open, task, onClose }: TaskDetailDrawerProps) {
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

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div className="task-detail-drawer-backdrop" onClick={onClose} />

      {/* Drawer panel */}
      <div className="task-detail-drawer" role="dialog" aria-label="Task Detail">
        <div className="task-detail-drawer-header">
          <span className="task-detail-drawer-title">Task Detail</span>
          <button className="task-detail-drawer-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="task-detail-drawer-body">
          <TaskDetailPane task={task} />
        </div>
      </div>
    </>
  );
}
