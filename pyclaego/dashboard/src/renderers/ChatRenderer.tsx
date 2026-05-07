/**
 * ChatRenderer — custom renderer for chat_log schema nodes.
 *
 * Messages and task records live in useLiveStore (Zustand), keyed by
 * `${psId}:${widgetId}`. State survives drawer close/reopen.
 *
 * Editor: CodeMirror 6 with markdown language.
 *   - Enter = newline
 *   - Cmd/Ctrl+Enter = send
 *   - Top edge of input area is draggable to resize editor height
 *   - Image attach button: sends multimodal content_parts to backend
 */

import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeMirror from '@uiw/react-codemirror';
import type { ReactCodeMirrorRef } from '@uiw/react-codemirror';
import { markdown } from '@codemirror/lang-markdown';
import { EditorView, keymap as cmKeymap } from '@codemirror/view';
import { Prec } from '@codemirror/state';
import { bridge } from '../ws/bridge';
import { useLiveStore, type ChatMessage, type PendingQueryUI, type ToolCallInfo } from '../store/live';
import { useTasksStore } from '../store/tasks';
import { useDraftsStore } from '../store/drafts';
import { useWidgetHighlight } from '../queries/widgets';
import { useRecentMessages } from '../queries/messages';
import { useWidgetTasks } from '../queries/tasks';
import { api, type TaskNode } from '../api';
import { TaskList } from '../primitives/TaskList';
import { MermaidBlock } from '../primitives/MermaidBlock';
import CronSection from '../components/CronSection';
import HistoryModal from '../components/HistoryModal';
import { TaskDetailDrawer } from '../components/TaskDetailDrawer';
import { StreamBox } from '../components/StreamBox';
import { StreamSidebar } from '../components/StreamSidebar';
import { WidgetTaskTreeDrawer } from '../components/WidgetTaskTreeDrawer';
import { WidgetSettingsModal } from '../components/WidgetSettingsModal';

// ---- Copy button (per-message) ------------------------------------------
export function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // fallback for insecure contexts
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  }, [text]);
  return (
    <button className="chat-copy-btn" onClick={handleCopy} title="Copy to clipboard">
      {copied ? '✓' : '⎘'}
    </button>
  );
}

// Stable empty arrays so Zustand snapshot comparisons don't create new refs.
const EMPTY_MESSAGES: ChatMessage[] = [];

// ---- Query prompt (interactive confirmation) ----------------------------
export interface QueryPromptProps {
  query: PendingQueryUI;
  psId: string;
  widgetId: string;
  storeKey: string;
  queueDepth: number;
}
export function QueryPrompt({ query, psId, widgetId, storeKey, queueDepth }: QueryPromptProps) {
  const addMessage = useLiveStore((s) => s.addMessage);

  const sendChoice = useCallback((value: string, label: string) => {
    // Show the user's choice as a chat bubble immediately
    addMessage(storeKey, {
      id: `q${Date.now()}`,
      role: 'user',
      text: label,
      timestamp: Date.now(),
    });
    // Send the machine-readable value as a chat message — PSGateway routes it
    // through try_resolve which resolves the pending future.
    bridge.send({ type: 'chat', ps_id: psId, widget_id: widgetId, content: value });
  }, [addMessage, storeKey, psId, widgetId]);

  return (
    <div className="query-prompt">
      {queueDepth > 0 && (
        <div
          className="query-prompt-queue-badge"
          title={`还有 ${queueDepth} 个询问在排队，回答后会依次出现`}
        >
          +{queueDepth}
        </div>
      )}
      {query.toolName && (
        <div className="query-prompt-context">
          <span className="query-prompt-icon">🔒</span>
          <span className="query-prompt-tool">{query.toolName}</span>
          {query.origin === 'rule' && <span className="query-prompt-badge">security rule</span>}
          {query.origin === 'tool' && <span className="query-prompt-badge">agent query</span>}
        </div>
      )}
      <div className="query-prompt-text markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {query.prompt}
        </ReactMarkdown>
      </div>
      <div className="query-prompt-choices">
        {query.choices.map((c) => (
          <button
            key={c.value}
            className="query-choice-btn"
            onClick={() => sendChoice(c.value, c.label)}
            title={c.description || undefined}
          >
            {c.label}
          </button>
        ))}
      </div>
    </div>
  );
}

interface Attachment {
  id: string;
  name: string;
  dataUrl: string;
}

interface ChatRendererProps {
  psId: string;
  widgetId: string;
  onCommand?: (command: string, args?: Record<string, unknown>) => void;
}

export function ChatRenderer({ psId, widgetId }: ChatRendererProps) {
  const storeKey = `${psId}:${widgetId}`;

  // ---- Store selectors (stable references) --------------------------------
  const messages     = useLiveStore((s) => s.messages[storeKey] ?? EMPTY_MESSAGES);
  const tasks        = useWidgetTasks(psId, widgetId);
  const pendingQuery = useLiveStore((s) => s.pendingQueries[storeKey] ?? null);
  const pendingQueueDepth = useLiveStore((s) => s.pendingQueryQueueDepth[storeKey] ?? 0);
  const addMessage    = useLiveStore((s) => s.addMessage);
  const seedMessages  = useLiveStore((s) => s.seedMessages);
  const historyFetched = useLiveStore((s) => s.historyFetched);

  // Fetch recent history once on first mount (disabled after first attempt)
  const historyEnabled = !historyFetched.has(storeKey);
  const { data: historyData } = useRecentMessages(psId, widgetId, historyEnabled);

  // Seed store when history fetch resolves (no-op if WS already populated).
  // Stream messages arrive exclusively via WebSocket — no REST fallback.
  useEffect(() => {
    if (!historyData) return;
    seedMessages(storeKey, historyData);
  }, [historyData, storeKey, seedMessages]);

  const draft      = useDraftsStore((s) => s.drafts[widgetId] ?? '');
  const setDraft   = useDraftsStore((s) => s.setDraft);
  const clearDraft = useDraftsStore((s) => s.clearDraft);

  // Fetch highlight for llm display (already cached by WidgetCardWrapper)
  const { data: highlight = {} } = useWidgetHighlight(psId, widgetId);
  const llmId = (highlight as Record<string, unknown>).llm as string | undefined;

  // Typing indicator — derived from busy flag (backend-managed via
  // widget.status_changed).  Covers ALL termination paths uniformly:
  // success / fail / cancel / error — backend always sends busy:false
  // in the finally block of _dispatch_chat.
  const typing = useLiveStore((s) => s.busy[storeKey] ?? false);

  // WS connection state
  const [connected, setConnected] = useState(() => bridge.connected);

  // Input area resize (draggable top handle)
  const [editorHeight, setEditorHeight] = useState(120);

  // Image attachments
  const [attachments, setAttachments] = useState<Attachment[]>([]);

  // History modal
  const [showHistory, setShowHistory] = useState(false);

  // Settings modal
  const [showSettings, setShowSettings] = useState(false);

  // Task tree drawer
  const [treeOpen, setTreeOpen] = useState(false);

  // Task detail drawer
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);

  // Flat map of task_id → TaskNode for detail lookup (reactive via Zustand selector)
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
    // Rebuild map whenever tasks for this widget change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [psId, widgetId, tasks]);

  const selectedTask = detailTaskId ? (taskNodeMap.get(detailTaskId) ?? null) : null;

  const handleTaskDetail = useCallback((taskId: string) => {
    setDetailTaskId(taskId);
  }, []);

  // ── Task Stream 按钮：从 artifact API 拉取流文本 → 左侧侧边栏 ────
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
      // 静默降级：获取失败不弹窗
    }
  }, []);

  // StreamBox sidebar
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarMessage, setSidebarMessage] = useState<ChatMessage | null>(null);

  // Refs
  const scrollRef    = useRef<HTMLDivElement>(null);
  const editorRef    = useRef<ReactCodeMirrorRef>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Stable ref to sendMessage so the keymap extension never needs updating
  const sendRef      = useRef<() => void>(() => {});

  // ---- Smart scroll -------------------------------------------------------
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

  // ---- WS connection state ------------------------------------------------
  useEffect(() => {
    setConnected(bridge.connected);
    const unsub = bridge.onConnectedChange(setConnected);
    return unsub;
  }, []);

  // ---- Send ---------------------------------------------------------------
  const sendMessage = useCallback(() => {
    const text = draft.trim();
    if (!text || !connected) return;

    // Intercept /stop: send as control frame (bypasses _processing_lock server-side)
    if (text === '/stop') {
      clearDraft(widgetId);
      bridge.send({ type: 'control', action: 'stop', ps_id: psId, widget_id: widgetId });
      return;
    }

    const requestId = `${widgetId}_${Date.now()}`;

    // Build display text for the user bubble
    const displayText = attachments.length > 0
      ? text
      : text;

    addMessage(storeKey, {
      id: `u${Date.now()}`,
      role: 'user',
      text: displayText,
      imageCount: attachments.length || undefined,
    });
    useLiveStore.getState().setBusy(storeKey, true);  // 前端乐观更新，消除到后端 busy:true 事件的延迟
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

  // Keep stable ref in sync
  useEffect(() => { sendRef.current = sendMessage; }, [sendMessage]);

  // ---- Stop ---------------------------------------------------------------
  const stopTask = useCallback(() => {
    bridge.send({ type: 'control', action: 'stop', ps_id: psId, widget_id: widgetId });
  }, [psId, widgetId]);

  // ---- Resize handle ------------------------------------------------------
  const handleResizeStart = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = editorHeight;
    const onMove = (ev: PointerEvent) => {
      // Drag up (negative delta in screen coords) = taller editor
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

  // ---- CodeMirror extensions (stable — use sendRef to avoid recreation) ---
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

  // ---- Render -------------------------------------------------------------
  const taskListItems = tasks.map((t) => ({
    id: t.id, label: t.label, status: t.status,
    startedAt: t.startedAt, duration: t.duration, error: t.error,
  }));

  const metaParts = [psId, widgetId, llmId].filter(Boolean).join(' · ');

  return (
    <div className="chat-renderer">
      {/* Left: chat log + input */}
      <div className="chat-panel">

        {/* Metadata bar */}
        <div className="chat-meta-bar">
          <span>chat · {metaParts}</span>
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

        {!connected && <div className="chat-reconnecting">Reconnecting…</div>}

        {/* Active streaming messages → StreamBox overlay */}
        {messages
          .filter((m: ChatMessage) => m.role === 'assistant' && m.streaming)
          .map((m: ChatMessage) => (
            <StreamBox
              key={m.id}
              message={m}
              onExpand={() => {
                setSidebarMessage(m);
                setSidebarOpen(true);
              }}
            />
          ))}

        {/* Message log */}
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
              psId={psId}
              widgetId={widgetId}
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
            onChange={(val) => setDraft(widgetId, val)}
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

      {/* Right: Task records + Cron */}
      <div className="chat-tasks-panel">
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
        <TaskList type="task_list" widget_id={widgetId} tasks={taskListItems} onTaskStream={handleTaskStream} />
        <div className="chat-tasks-divider" />
        <CronSection psId={psId} widgetId={widgetId} />
      </div>

      {/* History modal */}
      <HistoryModal
        open={showHistory}
        psId={psId}
        widgetId={widgetId}
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
        psId={psId}
        widgetId={widgetId}
        onClose={() => setTreeOpen(false)}
      />

      {/* Settings modal */}
      <WidgetSettingsModal
        open={showSettings}
        psId={psId}
        widgetId={widgetId}
        onClose={() => setShowSettings(false)}
      />
    </div>
  );
}

