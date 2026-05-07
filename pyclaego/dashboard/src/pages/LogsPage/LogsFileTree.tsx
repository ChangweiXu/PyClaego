import type { LogTreeNode } from './logsApi';
import { useLogsStore } from './logsStore';

interface Props {
  onFileOpen: (node: LogTreeNode) => void;
}

export default function LogsFileTree({ onFileOpen }: Props) {
  const tree = useLogsStore((s) => s.tree);

  if (tree.length === 0) {
    return <div className="logs-tree-empty">No log files yet</div>;
  }

  return (
    <ul className="logs-tree-root">
      {tree.map((node) => (
        <LogsTreeNode key={node.path} node={node} depth={0} onFileOpen={onFileOpen} />
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Recursive tree node
// ---------------------------------------------------------------------------

interface NodeProps {
  node: LogTreeNode;
  depth: number;
  onFileOpen: (node: LogTreeNode) => void;
}

function LogsTreeNode({ node, depth, onFileOpen }: NodeProps) {
  const expandedPaths = useLogsStore((s) => s.expandedPaths);
  const toggleExpand = useLogsStore((s) => s.toggleExpand);
  const activeTabPath = useLogsStore((s) => s.activeTabPath);

  const isExpanded = expandedPaths.has(node.path);
  const isActive = activeTabPath === node.path;
  const indent = depth * 14;

  if (node.type === 'directory') {
    return (
      <li>
        <button
          className="logs-tree-dir"
          style={{ paddingLeft: `${8 + indent}px` }}
          onClick={() => toggleExpand(node.path)}
          title={node.path}
        >
          <span className="logs-tree-icon">{isExpanded ? '▾' : '▸'}</span>
          <span className="logs-tree-label">{node.name}</span>
        </button>
        {isExpanded && node.children && node.children.length > 0 && (
          <ul className="logs-tree-children">
            {node.children.map((child) => (
              <LogsTreeNode
                key={child.path}
                node={child}
                depth={depth + 1}
                onFileOpen={onFileOpen}
              />
            ))}
          </ul>
        )}
      </li>
    );
  }

  // File node
  const ext = node.name.split('.').pop()?.toLowerCase() ?? '';
  const icon = ext === 'json' || ext === 'jsonl' ? '{}' : '≡';

  return (
    <li>
      <button
        className={`logs-tree-file${isActive ? ' logs-tree-file--active' : ''}`}
        style={{ paddingLeft: `${8 + indent}px` }}
        onClick={() => onFileOpen(node)}
        title={node.path}
      >
        <span className="logs-tree-icon logs-tree-icon--file">{icon}</span>
        <span className="logs-tree-label">{node.name}</span>
      </button>
    </li>
  );
}
