/**
 * StandaloneChatPage — full-page 3-column chat view.
 *
 * Layout: AgentTree | ChatLog | Tasks/Cron
 *
 * Route: /dashboard/chat/:psId/:widgetId
 * Opened via ChatRenderer's "⧉" button (window.open).
 *
 * Mirrors ChatRenderer features:
 *   - Top bar: Settings + History buttons
 *   - Input: CodeMirror + drag-to-resize + image attach
 *   - Right panel: Tree button, TaskDetail drawer, StreamSidebar
 *   - Query prompt card (pending_query)
 *   - CopyBtn + MermaidBlock on messages
 */

import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeMirror from '@uiw/react-codemirror';
import type { ReactCodeMirrorRef } from '@uiw/react-codemirror';
import { markdown } from '@codemirror/lang-markdown';
import { EditorView, keymap as cmKeymap } from '@codemirror/view';
import { Prec } from '@codemirror/state';
import { bridge } from '../../ws/bridge';
import { useLiveStore, type ChatMessage, type ToolCallInfo } from '../../store/live';
import { useTasksStore } from '../../store/tasks';
import { useDraftsStore } from '../../store/drafts';
import { useWidgetTasks } from '../../queries/tasks';
import { useWidgetHighlight } from '../../queries/widgets';
import { useRecentMessages } from '../../queries/messages';
import { api, type TaskNode } from '../../api';
import { TaskList } from '../../primitives/TaskList';
import { MermaidBlock } from '../../primitives/MermaidBlock';
import CronSection from '../../components/CronSection';
import HistoryModal from '../../components/HistoryModal';
import { TaskDetailDrawer } from '../../components/TaskDetailDrawer';
import { StreamSidebar } from '../../components/StreamSidebar';
import { WidgetTaskTreeDrawer } from '../../components/WidgetTaskTreeDrawer';
import { WidgetSettingsModal } from '../../components/WidgetSettingsModal';
import { CopyBtn, QueryPrompt } from '../../renderers/ChatRenderer';
import { AgentTreePanel } from './AgentTreePanel';
import { AgentStreamDrawer } from './AgentStreamDrawer';
import type { AgentStreamState } from '../../store/agentStreams';

interface Attachment {
  id: string;
  name: string;
  dataUrl: string;
}

const EMPTY_MESSAGES: ChatMessage[] = [];

export function StandaloneChatPage() {
  const { psId, widgetId } = useParams<{ psId: string; widgetId: string }>();
  const storeKey = `${psId}:${widgetId}`;

  // ---- Data ---------------------------------------------------------------
  const messages = useLiveStore((s) => s.messages[storeKey] ?? EMPTY_MESSAGES);
  const tasks = useWidgetTasks(psId!, widgetId!);
  const typing = useLiveStore((s) => s.busy[storeKey] ?? false);
  const historyFetched = useLiveStore((s) => s.historyFetched);
  const seedMessages = useLiveStore((s) => s.seedMessages);
  const addMessage = useLiveStore((s) => s.addMessage);
  const pendingQuery = useLiveStore((s) => s.pendingQueries[storeKey] ?? null);
  const pendingQueueDepth = useLiveStore((s) => s.pendingQueryQueueDepth[storeKey] ?? 0);

  const historyEnabled = !historyFetched.has(storeKey);
  const { data: historyData } = useRecentMessages(psId!, widgetId!, historyEnabled);

  useEffect(() => {
    if (!historyData) return;
    seedMessages(storeKey, historyData);
  }, [historyData, storeKey, seedMessages]);

  // Ensure WS is open for this PS
  useEffect(() => {
    if (!psId) return;
    bridge.start();
    bridge.ensurePSOpen(psId);
  }, [psId]);

  // ---- Draft --------------------------------------------------------------
  const draft = useDraftsStore((s) => s.drafts[widgetId!] ?? '');
  const setDraft = useDraftsStore((s) => s.setDraft);
  const clearDraft = useDraftsStore((s) => s.clearDraft);

  // ---- LLM display --------------------------------------------------------
  const { data: highlight = {} } = useWidgetHighlight(psId!, widgetId!);
  const llmId = (highlight as Record<string, unknown>).llm as string | undefined;

  // ---- Connection state ---------------------------------------------------
  const [connected, setConnected] = useState(() => bridge.connected);
  useEffect(() => {
    setConnected(bridge.connected);
    return bridge.onConnectedChange(setConnected);
  }, []);

  // ---- Input area ---------------------------------------------------------
  const [editorHeight, setEditorHeight] = useState(120);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const editorRef = useRef<ReactCodeMirrorRef>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const sendRef = useRef<() => void>(() => {});

  // ---- Modals & drawers ---------------------------------------------------
  const [showSettings, setShowSettings] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [treeOpen, setTreeOpen] = useState(false);
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarMessage, setSidebarMessage] = useState<ChatMessage | null>(null);

  // ---- Task node map (for TaskDetailDrawer) --------------------------------
  const taskNodeMap = useMemo(() => {
    const sessionKey = `${psId}__${widgetId}`;
    const roots = useTasksStore.getState().sessions[sessionKey] ?? [];
    const map = new Map<string, TaskNode>();
    const walk = (nodes: TaskNode[]) => {
      for (const n of nodes) {
        map.set(n.task_id, n);
        if (n.children) walk(n.children);
      }
    };
    walk(roots);
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [psId, widgetId, tasks]);

  const selectedTask = detailTaskId ? (taskNodeMap.get(detailTaskId) ?? null) : null;

  const handleTaskDetail = useCallback((taskId: string) => {
    setDetailTaskId(taskId);
  }, []);

  const handleTaskStream = useCallback(async (taskId: string) => {
    try {
      const { artifacts } = await api.getTaskArtifacts(taskId);
      const streamArtifact = artifacts.find((a) => a.kind === 'stream_content');
      if (!streamArtifact) return;
      const blob = await api.getTaskArtifactBlob(taskId, streamArtifact.artifact_id);
      const msg: ChatMessage = {
        id: `stream-${taskId}`,
        role: 'assistant',
        text: blob.text,
      };
      setSidebarMessage(msg);
      setSidebarOpen(true);
    } catch {
      // silent degradation
    }
  }, []);

  // ---- Scroll -------------------------------------------------------------
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [newMsgCount, setNewMsgCount] = useState(0);
  const prevMsgLenRef = useRef(0);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    setIsAtBottom(true);
    setNewMsgCount(0);
  }, []);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    setIsAtBottom(atBottom);
    if (atBottom) setNewMsgCount(0);
  }, []);

  useEffect(() => {
    const added = messages.length - prevMsgLenRef.current;
    prevMsgLenRef.current = messages.length;
    if (isAtBottom) {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
    } else if (added > 0) {
      setNewMsgCount((n) => n + added);
    }
  }, [messages, typing]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Send ---------------------------------------------------------------
  const sendMessage = useCallback(() => {
    const text = draft.trim();
    if (!text || !connected || !psId || !widgetId) return;

    if (text === '/stop') {
      clearDraft(widgetId);
      bridge.send({ type: 'control', action: 'stop', ps_id: psId, widget_id: widgetId });
      return;
    }

    const requestId = `${widgetId}_${Date.now()}`;
    addMessage(storeKey, {
      id: `u${Date.now()}`,
      role: 'user',
      text,
      timestamp: Date.now(),
      imageCount: attachments.length || undefined,
    });
    useLiveStore.getState().setBusy(storeKey, true);
    clearDraft(widgetId);
    setAttachments([]);

    const msg: Parameters<typeof bridge.send>[0] = {
      type: 'chat',
      ps_id: psId,
      widget_id: widgetId,
      content: text,
      request_id: requestId,
    };

    if (attachments.length > 0) {
      msg.content_parts = [
        { type: 'text', text },
        ...attachments.map((a) => ({
          type: 'image_url' as const,
          image_url: { url: a.dataUrl },
        })),
      ];
    }

    bridge.send(msg);
  }, [draft, connected, psId, widgetId, storeKey, addMessage, clearDraft, attachments]);

  // Keep stable ref in sync so CodeMirror keymap never stales
  useEffect(() => { sendRef.current = sendMessage; }, [sendMessage]);

  // ---- Stop ---------------------------------------------------------------
  const stopTask = useCallback(() => {
    if (!psId || !widgetId) return;
    bridge.send({ type: 'control', action: 'stop', ps_id: psId, widget_id: widgetId });
  }, [psId, widgetId]);

  // ---- Resize handle ------------------------------------------------------
  const handleResizeStart = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = editorHeight;
    const onMove = (ev: PointerEvent) => {
      setEditorHeight(Math.max(80, Math.min(480, startH + (startY - ev.clientY))));
    };
    const onUp = () => {
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
    };
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
  }, [editorHeight]);

  // ---- Image upload -------------------------------------------------------
  const handleFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    Array.from(e.target.files ?? []).forEach((file) => {
      const reader = new FileReader();
      reader.onload = (ev) => {
        setAttachments((prev) => [
          ...prev,
          { id: `img-${Date.now()}-${file.name}`, name: file.name, dataUrl: ev.target!.result as string },
        ]);
      };
      reader.readAsDataURL(file);
    });
    e.target.value = '';
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  // ---- CodeMirror extensions ----------------------------------------------
  const extensions = useMemo(() => [
    markdown(),
    EditorView.lineWrapping,
    Prec.highest(
      cmKeymap.of([{
        key: 'Mod-Enter',
        run: () => { sendRef.current(); return true; },
      }]),
    ),
  ], []); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Agent Stream Drawer ------------------------------------------------
  const [drawerKey, setDrawerKey] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const handleAgentSelect = useCallback((_stream: AgentStreamState | null, key: string | null) => {
    setDrawerKey(key);
    setDrawerOpen(true);
  }, []);

  const handleDrawerClose = useCallback(() => setDrawerOpen(false), []);

  // ---- Render -------------------------------------------------------------
  const metaParts = [psId, widgetId, llmId].filter(Boolean).join(' · ');
  const taskListItems = tasks.map((t) => ({
    id: t.id, label: t.label, status: t.status,
    startedAt: t.startedAt, duration: t.duration, error: t.error,
  }));

  return (
    <div className="standalone-chat">
      {/* Left: Agent Tree */}
      <div className="standalone-chat-left">
        <AgentTreePanel
          psId={psId!}
          widgetId={widgetId!}
          selectedKey={drawerKey}
          onSelect={handleAgentSelect}
        />
      </div>

      {/* Center: Chat log */}
      <div className="standalone-chat-center">
        <div className="chat-meta-bar">
          <span>chat · {metaParts}</span>
          {!connected && <span className="chat-reconnecting">Reconnecting…</span>}
          <div className="chat-meta-bar-actions">
            <button
              className="chat-settings-btn"
              onClick={() => setShowSettings(true)}
              title="Widget Settings"
            >
              Settings
            </button>
            <button
              className="chat-history-btn"
              onClick={() => setShowHistory(true)}
              title="View full history"
            >
              History
            </button>
          </div>
        </div>

        <div className="chat-log-wrap">
        <div className="chat-log" ref={scrollRef} onScroll={handleScroll}>
          {messages.length === 0 && (
            <div className="chat-empty">No messages yet — say something!</div>
          )}
          {messages
            .filter((m: ChatMessage) => !m.streaming)
            .map((m: ChatMessage) => (
              <div key={m.id} className={`chat-bubble-wrap ${m.role}`}>
                {m.timestamp && (
                  <div className="chat-bubble-time">
                    {new Date(m.timestamp).toLocaleString(undefined, {
                      month: 'short', day: 'numeric',
                      hour: '2-digit', minute: '2-digit',
                    })}
                  </div>
                )}
                <div className={`chat-bubble ${m.role}`}>
                  {m.role === 'assistant' ? (
                    <div className="chat-text markdown-body">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{ code: MermaidBlock as any }}
                      >
                        {m.displayText || m.fullContent || m.text}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    <span className="chat-text">{m.text}</span>
                  )}
                  {m.toolCalls && m.toolCalls.length > 0 && (
                    <div className="chat-tool-calls">
                      {m.toolCalls.map((tc: ToolCallInfo) => (
                        <span
                          key={tc.id}
                          className={`chat-tool-call ${tc.status}`}
                          title={tc.status === 'running' ? `Using tool: ${tc.name}…` : `Completed: ${tc.name}`}
                        >
                          {tc.status === 'running' ? '⏳' : '✅'} {tc.name}
                        </span>
                      ))}
                    </div>
                  )}
                  {m.imageCount ? (
                    <span className="chat-img-badge">📎 {m.imageCount} image{m.imageCount > 1 ? 's' : ''}</span>
                  ) : null}
                </div>
                <CopyBtn text={m.displayText || m.fullContent || m.text} />
              </div>
            ))}
          {pendingQuery && (
            <QueryPrompt
              query={pendingQuery}
              psId={psId!}
              widgetId={widgetId!}
              storeKey={storeKey}
              queueDepth={pendingQueueDepth}
            />
          )}
          {typing && (
            <div className="chat-bubble assistant typing">
              <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
            </div>
          )}
        </div>
        {!isAtBottom && (
          <button className="chat-scroll-btn" onClick={scrollToBottom} title="Jump to bottom">
            ↓{newMsgCount > 0 && <span className="chat-scroll-badge">{newMsgCount}</span>}
          </button>
        )}
        </div>

        {/* Input area */}
        <div className="chat-input-area">
          {/* Drag-to-resize handle */}
          <div
            className="chat-input-resize-handle"
            onPointerDown={handleResizeStart}
            title="Drag to resize editor"
          />

          {/* Image previews */}
          {attachments.length > 0 && (
            <div className="chat-attach-preview">
              {attachments.map((a) => (
                <div key={a.id} className="chat-attach-thumb">
                  <img src={a.dataUrl} alt={a.name} />
                  <button
                    className="chat-attach-remove"
                    onClick={() => removeAttachment(a.id)}
                    title="Remove"
                  >×</button>
                </div>
              ))}
            </div>
          )}

          {/* Markdown editor */}
          <CodeMirror
            ref={editorRef}
            value={draft}
            onChange={(val) => setDraft(widgetId!, val)}
            extensions={extensions}
            placeholder={connected ? 'Message… (Cmd/Ctrl+Enter to send, Enter for newline)' : 'Reconnecting…'}
            editable={connected}
            height={`${editorHeight}px`}
            className="chat-codemirror"
            basicSetup={{
              lineNumbers: false,
              foldGutter: false,
              dropCursor: false,
              allowMultipleSelections: false,
              indentOnInput: false,
              searchKeymap: false,
            }}
          />

          {/* Action row */}
          <div className="chat-input-actions">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              style={{ display: 'none' }}
              onChange={handleFileChange}
            />
            <button
              className="chat-attach-btn"
              onClick={() => fileInputRef.current?.click()}
              disabled={!connected}
              title="Attach image (multimodal)"
            >🖼️</button>
            <span className="chat-input-hint">Cmd/Ctrl+Enter to send</span>
            <div style={{ flex: 1 }} />
            {typing && (
              <button className="chat-stop-btn" onClick={stopTask}>Stop</button>
            )}
            <button
              className="chat-send-btn"
              onClick={sendMessage}
              disabled={!draft.trim() || !connected}
            >Send</button>
          </div>
        </div>
      </div>

      {/* Right: Tasks + Cron */}
      <div className="standalone-chat-right">
        <div className="chat-tasks-header">
          <span>Task Records</span>
          <button
            className="chat-tree-btn"
            onClick={() => setTreeOpen(true)}
            title="Open task tree"
          >
            Tree
          </button>
        </div>
        <TaskList
          type="task_list"
          widget_id={widgetId!}
          tasks={taskListItems}
          onTaskStream={handleTaskStream}
          onTaskDetail={handleTaskDetail}
        />
        <div className="chat-tasks-divider" />
        <CronSection psId={psId!} widgetId={widgetId!} />
      </div>

      {/* Agent Stream Drawer (overlay on left) */}
      <AgentStreamDrawer
        open={drawerOpen}
        streamKey={drawerKey}
        onClose={handleDrawerClose}
      />

      {/* History modal */}
      <HistoryModal
        open={showHistory}
        psId={psId!}
        widgetId={widgetId!}
        onClose={() => setShowHistory(false)}
      />

      {/* Full-content sidebar */}
      <StreamSidebar
        open={sidebarOpen}
        message={sidebarMessage}
        onClose={() => setSidebarOpen(false)}
      />

      {/* Task detail drawer */}
      <TaskDetailDrawer
        open={detailTaskId !== null}
        task={selectedTask}
        onClose={() => setDetailTaskId(null)}
      />

      {/* Task tree drawer */}
      <WidgetTaskTreeDrawer
        open={treeOpen}
        psId={psId!}
        widgetId={widgetId!}
        onClose={() => setTreeOpen(false)}
      />

      {/* Settings modal */}
      <WidgetSettingsModal
        open={showSettings}
        psId={psId!}
        widgetId={widgetId!}
        onClose={() => setShowSettings(false)}
      />
    </div>
  );
}
