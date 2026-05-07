import { useState } from 'react';
import type { TreeSchema, TreeNode } from '../schema/types';

interface TreeProps extends TreeSchema {
  onCommand?: (command: string, args?: Record<string, unknown>) => void;
}

export function Tree({ nodes, on_select_command, onCommand }: TreeProps) {
  return (
    <ul className="p-tree">
      {nodes.map((n) => (
        <TreeItem key={n.id} node={n} onSelectCommand={on_select_command} onCommand={onCommand} />
      ))}
    </ul>
  );
}

function TreeItem({
  node,
  onSelectCommand,
  onCommand,
}: {
  node: TreeNode;
  onSelectCommand?: string;
  onCommand?: (command: string, args?: Record<string, unknown>) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = node.children && node.children.length > 0;

  const handleClick = () => {
    if (hasChildren) setExpanded((v) => !v);
    if (onSelectCommand && onCommand) {
      onCommand(onSelectCommand, { node_id: node.id, payload: node.payload });
    }
  };

  return (
    <li className={`p-tree-item ${hasChildren ? 'has-children' : ''}`}>
      <div className="p-tree-label" onClick={handleClick}>
        {hasChildren && (
          <span className="p-tree-arrow">{expanded ? '▾' : '▸'}</span>
        )}
        {node.icon && <span className="p-tree-icon">{node.icon}</span>}
        <span className="p-tree-text">{node.label}</span>
      </div>
      {hasChildren && expanded && (
        <ul className="p-tree-children">
          {node.children!.map((child) => (
            <TreeItem key={child.id} node={child} onSelectCommand={onSelectCommand} onCommand={onCommand} />
          ))}
        </ul>
      )}
    </li>
  );
}
