/**
 * TaskTree — recursive collapsible task tree renderer.
 *
 * Renders a list of TaskNode trees with icons, status badges, and
 * expandable children. Top-level nodes default to expanded (depth 0 & 1);
 * deeper nodes start collapsed.
 *
 * Row interaction: clicking any row selects it and toggles expand/collapse
 * (if it has children). Both actions fire on the same click.
 */
import { useState } from 'react';
import type { TaskNode } from '../api';
import { useUIStore } from '../store/ui';

const TYPE_ICON: Record<string, string> = {
  user_message:   '💬',
  agent_loop:     '🔁',
  tool_execution: '🔧',
  subagent_spawn: '🧬',
  subagent_loop:  '🔄',
  llm_call:       '🧠',
  memory_read:    '📖',
  memory_write:   '💾',
  memory_search:  '🔍',
};

const STATUS_LABEL: Record<string, string> = {
  pending:   '⏳',
  running:   '…',
  completed: '✓',
  failed:    '✗',
  cancelled: '–',
};

function TaskNodeRow({ node, depth }: { node: TaskNode; depth: number }) {
  const [expanded, setExpanded] = useState(depth < 2);
  const hasChildren = (node.children?.length ?? 0) > 0;
  const icon = TYPE_ICON[node.task_type] ?? '⚙️';

  const selectedTaskId = useUIStore((s) => s.selectedTaskId);
  const selectTask = useUIStore((s) => s.selectTask);
  const isSelected = selectedTaskId === node.task_id;

  return (
    <div className="task-node">
      <div
        className={`task-node-row${hasChildren ? ' task-node-row--expandable' : ''}${isSelected ? ' task-node-row--selected' : ''}`}
        style={{ paddingLeft: `${depth * 14}px` }}
        onClick={() => {
          if (hasChildren) setExpanded((x) => !x);
          selectTask(node.task_id);
        }}
      >
        <span className="task-node-toggler">
          {hasChildren ? (expanded ? '▾' : '▸') : '·'}
        </span>
        <span className="task-node-icon">{icon}</span>
        <span className="task-node-name">{node.name || node.task_type}</span>
        <span className={`task-node-status task-node-status--${node.status}`}>
          {STATUS_LABEL[node.status] ?? node.status}
        </span>
      </div>
      {expanded && hasChildren && (
        <div className="task-node-children">
          {node.children!.map((child) => (
            <TaskNodeRow key={child.task_id} node={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

export function TaskTree({ nodes }: { nodes: TaskNode[] }) {
  if (nodes.length === 0) return null;
  return (
    <div className="task-tree">
      {nodes.map((n) => (
        <TaskNodeRow key={n.task_id} node={n} depth={0} />
      ))}
    </div>
  );
}
