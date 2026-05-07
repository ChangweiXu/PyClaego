import { useState, useCallback } from 'react';
import type { FileTreeNode } from './notesRpc';

interface Props {
  tree: FileTreeNode[];
  activeRelPath?: string;
  onFileClick: (relPath: string) => void;
  onCreateFile: (parentDir: string) => void;
  onDeleteFile: (relPath: string) => void;
  onRenameFile: (relPath: string) => void;
}

export default function FileTree({
  tree,
  activeRelPath,
  onFileClick,
  onCreateFile,
  onDeleteFile,
  onRenameFile,
}: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [contextMenu, setContextMenu] = useState<{
    x: number; y: number; node: FileTreeNode;
  } | null>(null);

  const toggleDir = useCallback((relPath: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(relPath)) next.delete(relPath);
      else next.add(relPath);
      return next;
    });
  }, []);

  const collapseAll = useCallback(() => {
    const dirs = new Set<string>();
    function collect(nodes: FileTreeNode[]) {
      for (const n of nodes) {
        if (n.type === 'dir') {
          dirs.add(n.rel_path);
          if (n.children) collect(n.children);
        }
      }
    }
    collect(tree);
    setCollapsed(dirs);
  }, [tree]);

  function handleContextMenu(e: React.MouseEvent, node: FileTreeNode) {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, node });
  }

  function renderNode(node: FileTreeNode, depth: number): React.ReactNode {
    const indent = depth * 14;
    if (node.type === 'dir') {
      const isOpen = !collapsed.has(node.rel_path);
      return (
        <div key={node.rel_path}>
          <div
            className="notes-tree-row notes-tree-dir"
            style={{ paddingLeft: indent }}
            onClick={() => toggleDir(node.rel_path)}
            onContextMenu={(e) => handleContextMenu(e, node)}
          >
            <span className="notes-tree-arrow">{isOpen ? '▾' : '▸'}</span>
            <span className="notes-tree-icon">📁</span>
            <span className="notes-tree-name">{node.name}</span>
          </div>
          {isOpen && node.children?.map((child) => renderNode(child, depth + 1))}
        </div>
      );
    }
    const isActive = node.rel_path === activeRelPath;
    return (
      <div
        key={node.rel_path}
        className={`notes-tree-row notes-tree-file${isActive ? ' active' : ''}`}
        style={{ paddingLeft: indent + 16 }}
        onClick={() => onFileClick(node.rel_path)}
        onContextMenu={(e) => handleContextMenu(e, node)}
      >
        <span className="notes-tree-icon">📄</span>
        <span className="notes-tree-name">{node.name}</span>
      </div>
    );
  }

  return (
    <div
      className="notes-file-tree"
      onClick={() => contextMenu && setContextMenu(null)}
    >
      <div className="notes-tree-toolbar">
        <button className="notes-btn-ghost" onClick={collapseAll} title="Collapse all">
          ⊟ Collapse All
        </button>
      </div>
      <div className="notes-tree-body">
        {tree.map((node) => renderNode(node, 0))}
      </div>

      {contextMenu && (
        <div
          className="notes-context-menu"
          style={{ top: contextMenu.y, left: contextMenu.x }}
        >
          {contextMenu.node.type === 'dir' && (
            <button onClick={() => { onCreateFile(contextMenu.node.rel_path); setContextMenu(null); }}>
              ➕ New file here
            </button>
          )}
          {contextMenu.node.type === 'file' && (
            <>
              <button onClick={() => { onRenameFile(contextMenu.node.rel_path); setContextMenu(null); }}>
                ✏️ Rename
              </button>
              <button
                className="danger"
                onClick={() => { onDeleteFile(contextMenu.node.rel_path); setContextMenu(null); }}
              >
                🗑 Delete
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
