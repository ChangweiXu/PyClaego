import CodeMirror from '@uiw/react-codemirror';
import { javascript } from '@codemirror/lang-javascript';
import { EditorView } from '@codemirror/view';
import { useLogsStore } from './logsStore';

// ---------------------------------------------------------------------------
// Language detection
// ---------------------------------------------------------------------------

function detectExtensions(path: string) {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  // Use the JavaScript language mode with json:true for JSON/JSONL files;
  // @codemirror/lang-json is not installed but lang-javascript covers JSON syntax.
  if (ext === 'json' || ext === 'jsonl') return [javascript({ jsx: false })];
  return [];
}

// ---------------------------------------------------------------------------
// Tab bar
// ---------------------------------------------------------------------------

function LogsTabBar() {
  const openTabs = useLogsStore((s) => s.openTabs);
  const activeTabPath = useLogsStore((s) => s.activeTabPath);
  const setActiveTab = useLogsStore((s) => s.setActiveTab);
  const closeTab = useLogsStore((s) => s.closeTab);

  if (openTabs.length === 0) return null;

  return (
    <div className="logs-tabbar">
      {openTabs.map((tab) => (
        <div
          key={tab.path}
          className={`logs-tab${tab.path === activeTabPath ? ' logs-tab--active' : ''}`}
          onClick={() => setActiveTab(tab.path)}
          title={tab.path}
        >
          <span className="logs-tab-label">{tab.label}</span>
          <button
            className="logs-tab-close"
            onClick={(e) => {
              e.stopPropagation();
              closeTab(tab.path);
            }}
            aria-label={`Close ${tab.label}`}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main viewer
// ---------------------------------------------------------------------------

export default function LogsViewer() {
  const openTabs = useLogsStore((s) => s.openTabs);
  const activeTabPath = useLogsStore((s) => s.activeTabPath);
  const wrapLines = useLogsStore((s) => s.wrapLines);
  const toggleWrapLines = useLogsStore((s) => s.toggleWrapLines);

  const activeTab = openTabs.find((t) => t.path === activeTabPath) ?? null;

  const extensions = [
    ...(activeTab ? detectExtensions(activeTab.path) : []),
    ...(wrapLines ? [EditorView.lineWrapping] : []),
  ];

  return (
    <div className="logs-viewer">
      <LogsTabBar />

      <div className="logs-editor-area">
        {activeTab === null ? (
          <div className="logs-empty-state">Select a file from the tree to view it</div>
        ) : activeTab.loading ? (
          <div className="logs-empty-state">Loading…</div>
        ) : activeTab.error ? (
          <div className="logs-error-state">{activeTab.error}</div>
        ) : (
          <CodeMirror
            value={activeTab.content}
            extensions={extensions}
            editable={false}
            basicSetup={{
              lineNumbers: true,
              foldGutter: true,
              highlightActiveLine: false,
            }}
            className="logs-codemirror"
            height="100%"
          />
        )}
      </div>

      <div className="logs-statusbar">
        <button
          className={`logs-wrap-btn${wrapLines ? ' logs-wrap-btn--active' : ''}`}
          onClick={toggleWrapLines}
          title="Toggle line wrapping"
        >
          ↵ Wrap line
        </button>
        {activeTab && !activeTab.loading && !activeTab.error && (
          <span className="logs-statusbar-info">{activeTab.path}</span>
        )}
      </div>
    </div>
  );
}
