import { useEffect, useCallback } from 'react';
import { logsApi, type LogTreeNode } from './logsApi';
import { logsWS } from '../../ws/logsWS';
import { useLogsStore } from './logsStore';
import LogsFileTree from './LogsFileTree';
import LogsViewer from './LogsViewer';
import './logs.css';

export default function LogsPage() {
  const setTree = useLogsStore((s) => s.setTree);
  const collapseAll = useLogsStore((s) => s.collapseAll);
  const closeAllTabs = useLogsStore((s) => s.closeAllTabs);
  const openTab = useLogsStore((s) => s.openTab);
  const setTabContent = useLogsStore((s) => s.setTabContent);
  const setTabError = useLogsStore((s) => s.setTabError);

  // Initial tree fetch + WebSocket watcher
  useEffect(() => {
    // Bootstrap with a REST call so there's no blank flash before WS connects
    logsApi.getTree()
      .then((r) => setTree(r.tree))
      .catch((e) => console.error('[LogsPage] getTree failed:', e));

    // WS pushes incremental tree updates
    const unsub = logsWS.onTree((tree) => setTree(tree));
    logsWS.start();

    return () => {
      unsub();
      logsWS.stop();
    };
  }, [setTree]);

  // Open a file: add tab (loading), then fetch content
  const handleFileOpen = useCallback(
    async (node: LogTreeNode) => {
      openTab(node.path, node.name);
      try {
        const res = await logsApi.getFile(node.path);
        setTabContent(node.path, res.content);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setTabError(node.path, msg);
      }
    },
    [openTab, setTabContent, setTabError]
  );

  return (
    <div className="logs-page">
      {/* Top toolbar */}
      <div className="logs-topbar">
        <span className="logs-topbar-title">📂 Logs</span>
        <button className="logs-topbar-btn" onClick={collapseAll} title="Collapse all folders">
          Collapse all
        </button>
        <button className="logs-topbar-btn" onClick={closeAllTabs} title="Close all file tabs">
          Close all
        </button>
      </div>

      {/* Body: sidebar + main */}
      <div className="logs-body">
        <aside className="logs-sidebar">
          <LogsFileTree onFileOpen={handleFileOpen} />
        </aside>
        <div className="logs-main">
          <LogsViewer />
        </div>
      </div>
    </div>
  );
}
