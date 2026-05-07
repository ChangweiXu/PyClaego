/**
 * AgentTreePanel — collapsible agent tree for the standalone chat page.
 *
 * Data source: backend-built agent tree from GET /api/tasks/agent-tree.
 * The backend walks the full TaskNode tree, identifies SUBAGENT_SPAWN tasks,
 * and returns a clean Main Agent → SubAgent → ... hierarchy.
 *
 * Each row has a StreamButton on the right side.
 */

import { useState, useCallback } from 'react';
import { useAgentTree } from '../../queries/tasks';
import { useAgentStreamStore, type AgentStreamState } from '../../store/agentStreams';
import { useAgentStreamHydration } from '../../queries/agentStreams';
import { StreamButton } from '../../components/StreamButton';
import type { AgentNode } from '../../api';

interface AgentTreePanelProps {
  psId: string;
  widgetId: string;
  selectedKey: string | null;
  onSelect: (stream: AgentStreamState | null, key: string | null) => void;
}

export function AgentTreePanel({ psId, widgetId, selectedKey, onSelect }: AgentTreePanelProps) {
  const { agents } = useAgentTree(psId, widgetId);
  const streams = useAgentStreamStore((s) => s.streams);

  // 页面刷新后从 REST API 还原 stream 历史到 store
  useAgentStreamHydration(psId, widgetId, agents);

  // Collapsible state per agent id
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggleCollapse = useCallback((id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Find stream for a given agent
  const getStream = useCallback(
    (reqId: string, subagentId?: string | null): AgentStreamState | null => {
      const prefix = `${psId}:${widgetId}:${reqId}:`;
      for (const [key, st] of Object.entries(streams)) {
        if (key.startsWith(prefix)) {
          if (subagentId ? key.endsWith(`:${subagentId}`) : key.endsWith(':_main')) return st;
        }
      }
      return null;
    },
    [psId, widgetId, streams],
  );

  const handleStreamClick = useCallback(
    (reqId: string, subagentId: string | null) => {
      const stream = getStream(reqId, subagentId);
      const key = `${psId}:${widgetId}:${reqId}:${subagentId ?? '_main'}`;
      onSelect(stream || null, key);
    },
    [psId, widgetId, getStream, onSelect],
  );

  const renderRow = (node: AgentNode, depth: number): JSX.Element => {
    const isCollapsed = collapsed.has(node.id);
    const hasChildren = node.children.length > 0;
    const sid = node.subagent_id;
    const stream = getStream(node.request_id, sid);
    const active = selectedKey?.endsWith(`:${sid ?? '_main'}`) ?? false;

    return (
      <div key={node.id} className="agent-tree-row-wrap">
        <div className={`agent-tree-row${active ? ' agent-tree-row--selected' : ''}`}
             style={{ paddingLeft: `${12 + depth * 16}px` }}>
          {/* Collapse toggle */}
          <button
            className={`agent-tree-toggle${!hasChildren ? ' agent-tree-toggle--hidden' : ''}`}
            onClick={() => hasChildren && toggleCollapse(node.id)}
          >
            {hasChildren ? (isCollapsed ? '▶' : '▼') : ' '}
          </button>

          {/* Label */}
          <span className="agent-tree-label">{node.label}</span>

          {/* Stream button */}
          <StreamButton
            stream={stream}
            active={active}
            onClick={() => handleStreamClick(node.request_id, sid)}
          />
        </div>

        {/* Children (if not collapsed) */}
        {hasChildren && !isCollapsed &&
          node.children.map((child) => renderRow(child, depth + 1))}
      </div>
    );
  };

  if (agents.length === 0) {
    return (
      <div className="agent-tree-empty">
        No agent activity yet — send a message to start.
      </div>
    );
  }

  return (
    <div className="agent-tree-panel">
      <div className="agent-tree-header">Agents</div>
      {agents.map((root) => renderRow(root, 0))}
    </div>
  );
}
