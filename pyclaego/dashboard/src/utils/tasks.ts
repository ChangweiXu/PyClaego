/** Shared task-tree utility functions. */
import type { TaskNode } from '../api';

/** Split a session_id into display chips (split on '__'). */
export function sessionChips(sessionId: string): string[] {
  return sessionId.split('__').filter(Boolean);
}

/** Turn label: prefer name, fall back to task_type. */
export function turnLabel(turn: TaskNode): string {
  return turn.name || turn.task_type;
}

/** Depth-first search for a task node by id across a forest. */
export function findTaskById(nodes: TaskNode[], id: string): TaskNode | null {
  for (const node of nodes) {
    if (node.task_id === id) return node;
    if (node.children?.length) {
      const found = findTaskById(node.children, id);
      if (found) return found;
    }
  }
  return null;
}
