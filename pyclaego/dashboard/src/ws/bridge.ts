/**
 * ws/bridge.ts — single app-level WebSocket bridge.
 *
 * One persistent WS connection for the entire dashboard lifetime.
 * Opened once at app boot (bridge.start()); never closed on PS navigation.
 * Routes incoming events to either:
 *   - queryClient.setQueryData  (cache patches, e.g. status_changed, view_invalidated)
 *   - useLiveStore actions       (streaming chunks, transient busy flags)
 *
 * Usage:
 *   bridge.start()              — call once at app boot
 *   bridge.ensurePSOpen(psId)   — idempotent; call whenever entering a PS dashboard
 *   bridge.send(msg)            — send a PSMsg to the core server
 *   bridge.onReply(cb)          — subscribe to reply messages (chat responses)
 *   bridge.onConnectedChange(cb)— subscribe to WS connect/disconnect events
 *   bridge.shutdown()           — intentional teardown (hot-reload / app unmount only)
 *
 * The bridge auto-reconnects on unexpected close.
 * On reconnect it replays `open` for every PS seen in this session.
 */

import { queryClient } from '../queries/client';
import { useLiveStore } from '../store/live';
import { useAgentStreamStore } from '../store/agentStreams';
import type { PSMsg, PSReply } from '../ws';
const WS_URL = '/ws/v2/chat';
const RECONNECT_DELAYS = [1000, 2000, 5000, 10_000, 30_000]; // backoff steps (ms)

type ReplyListener = (msg: PSReply) => void;
type ConnectedListener = (connected: boolean) => void;

class WSBridge {
  private ws: WebSocket | null = null;
  /** All PSes opened in this session — replayed on reconnect. */
  private openedPSes = new Set<string>();
  /** Per-request last seen seq → used for resume_streams on reconnect. */
  private _lastSeqByRequest = new Map<string, number>();
  /** Per-request widget_id → needed to build resume_streams by widget. */
  private _requestWidget = new Map<string, string>();
  private retryCount = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<ReplyListener>();
  private connListeners = new Set<ConnectedListener>();
  private intentionallyClosed = false;
  private _msgCounter = 0;
  private _newMsgId() { return `bm${++this._msgCounter}`; }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /** Open the WS connection once at app boot. Idempotent. */
  start(): void {
    if (this.ws !== null) return;
    this.intentionallyClosed = false;
    this._connect();
  }

  /**
   * Register interest in a PS. Idempotent.
   * Sends {type:'open', ps_id} immediately if already connected,
   * or queues it (onopen replays all openedPSes on connect / reconnect).
   */
  ensurePSOpen(psId: string): void {
    if (this.openedPSes.has(psId)) return;
    this.openedPSes.add(psId);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({ type: 'open', ps_id: psId });
    }
  }

  /** Intentional teardown — only for hot-reload or full app unmount. */
  shutdown(): void {
    this.intentionallyClosed = true;
    this._clearRetryTimer();
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
  }

  send(msg: PSMsg): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[WSBridge] send() called but WS not open');
      return;
    }
    this.ws.send(JSON.stringify(msg));
  }

  /** Subscribe to raw reply messages (used by ChatRenderer for streaming). */
  onReply(cb: ReplyListener): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  /**
   * Subscribe to WS connection state changes.
   * Fires true when the socket opens, false when it closes unexpectedly.
   * Returns an unsubscribe function.
   */
  onConnectedChange(cb: ConnectedListener): () => void {
    this.connListeners.add(cb);
    return () => this.connListeners.delete(cb);
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private _connect(): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}${WS_URL}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.retryCount = 0;
      this.connListeners.forEach((l) => l(true));
      // Replay open for every PS seen in this session (handles reconnect after drop)
      this.openedPSes.forEach((psId) => {
        // 构建 resume_streams：按 widget_id 分组，每个 request 携带 last_seq
        const resumeStreams: Record<string, Record<string, number>> = {};
        this._requestWidget.forEach((wId, rId) => {
          const seq = this._lastSeqByRequest.get(rId);
          if (seq !== undefined && seq >= 0) {
            (resumeStreams[wId] ??= {})[rId] = seq;
          }
        });
        this.send({
          type: 'open',
          ps_id: psId,
          ...(Object.keys(resumeStreams).length > 0 ? { resume_streams: resumeStreams } : {}),
        });
      });
    };

    this.ws.onmessage = (ev) => {
      let msg: PSReply;
      try {
        msg = JSON.parse(ev.data as string) as PSReply;
      } catch {
        return;
      }
      this._dispatch(msg);
    };

    this.ws.onclose = () => {
      this.ws = null;
      // Notify subscribers so the UI can show a reconnecting state.
      if (!this.intentionallyClosed) {
        this.connListeners.forEach((l) => l(false));
      }
      if (!this.intentionallyClosed) {
        this._scheduleReconnect();
      }
    };

    this.ws.onerror = () => {
      // onclose fires after onerror — no extra handling needed
    };
  }

  private _dispatch(msg: PSReply): void {
    // 1. Notify raw listeners (ChatRenderer uses this only for typing indicator now)
    this.listeners.forEach((l) => l(msg));

    const liveS = useLiveStore.getState();

    // 2. Route event types to TQ cache or live store
    if (msg.type === 'event') {
      const event = msg as Record<string, unknown>;
      const evType = event['event'] as string | undefined;
      const psId = event['ps_id'] as string | undefined;
      const widgetId = event['widget_id'] as string | undefined;

      if (!psId || !widgetId) return;

      if (evType === 'widget.status_changed') {
        const status = event['status'] as string | undefined;
        const busy = event['busy'] as boolean | undefined;
        queryClient.setQueryData(
          ['widget', psId, widgetId, 'highlight'],
          (old: Record<string, unknown> | undefined) => ({
            ...(old ?? {}),
            ...(status !== undefined ? { status } : {}),
            ...(busy !== undefined ? { busy } : {}),
          }),
        );
        if (busy !== undefined) liveS.setBusy(`${psId}:${widgetId}`, busy);
      }

      if (evType === 'widget.view_invalidated') {
        queryClient.invalidateQueries({ queryKey: ['widget', psId, widgetId, 'view'] });
      }

      if (evType === 'widget.highlight_changed') {
        const highlight = event['highlight'] as Record<string, unknown> | undefined;
        if (highlight) queryClient.setQueryData(['widget', psId, widgetId, 'highlight'], highlight);
      }

      // ── Query confirmation events ──────────────────────────────────────
      const storeKey = `${psId}:${widgetId}`;

      if (evType === 'query.opened') {
        liveS.setPendingQuery(storeKey, {
          queryId:   event['query_id']  as string,
          sessionId: event['session_id'] as string,
          prompt:    event['prompt']    as string,
          choices:   event['choices']   as Array<{ value: string; label: string; description: string }>,
          default:   event['default']   as string | undefined,
          timeoutS:  event['timeout_s'] as number | undefined,
          origin:    event['origin']    as string | undefined,
          toolName:  event['tool_name'] as string | undefined,
        });
        // queue_depth includes the HEAD itself; badge shows the rest.
        const depth = (event['queue_depth'] as number | undefined) ?? 1;
        liveS.setPendingQueryDepth(storeKey, Math.max(0, depth - 1));
      }

      if (evType === 'query.queued') {
        // A non-HEAD query was enqueued; only update the badge counter.
        const depth = (event['queue_depth'] as number | undefined) ?? 0;
        liveS.setPendingQueryDepth(storeKey, Math.max(0, depth - 1));
      }

      if (evType === 'query.resolved') {
        // HEAD was resolved — clear current card; backend will broadcast a
        // fresh query.opened for the promoted next HEAD (if any).
        liveS.setPendingQuery(storeKey, null);
        // Conservatively clear depth; the next query.opened will set it.
        liveS.setPendingQueryDepth(storeKey, 0);
      }

      if (evType === 'query.cleared') {
        liveS.setPendingQuery(storeKey, null);
        liveS.setPendingQueryDepth(storeKey, 0);
      }
    }

    // 3. Accumulate reply messages directly into useLiveStore so history
    //    survives drawer close/reopen (ChatRenderer is not the only consumer).
    //    Use reply.ps_id — bridge now serves multiple PSes simultaneously.

    // ── ack 消息：处理 active_streams，标记已完成的流 ────────────
    if (msg.type === 'ack' && (msg as any).active_streams) {
      const liveSx = useLiveStore.getState();
      const agentS = useAgentStreamStore.getState();
      for (const st of (msg as any).active_streams) {
        if (st.subagent_id) {
          // 子 Agent 流已完成 → 标记 agentStreamStore
          if (st.finished) {
            const psId = (msg as any).ps_id ?? '';
            const requestId = st.request_id ?? '';
            agentS.endStream(psId, st.widget_id, requestId, st.subagent_id);
          }
        } else {
          // 主 Agent 流
          const storeKey = `${(msg as any).ps_id ?? ''}:${st.widget_id}`;
          if (st.finished) {
            liveSx.patchMessage(storeKey, st.request_id, { streaming: false });
          } else {
            // 流仍在进行中（页面刷新重连）→ 恢复 busy 状态，确保 Stop 按钮显示
            liveSx.setBusy(storeKey, true);
          }
        }
      }
    }

    // ── ack 消息：恢复刷新后的 pending queries（重建 QueryPrompt 卡片）───
    if (msg.type === 'ack' && (msg as any).pending_queries) {
      const liveSx = useLiveStore.getState();
      const pendingMap = (msg as any).pending_queries as Record<string, any[]>;
      const psId = (msg as any).ps_id ?? '';
      for (const [wId, queries] of Object.entries(pendingMap)) {
        if (!queries.length) continue;
        const storeKey = `${psId}:${wId}`;
        // HEAD query（索引 0）→ setPendingQuery
        const head = queries[0];
        liveSx.setPendingQuery(storeKey, {
          queryId:   head.query_id,
          sessionId: head.session_id,
          prompt:    head.prompt,
          choices:   head.choices,
          default:   head.default,
          timeoutS:  head.timeout_s,
          origin:    head.origin,
          toolName:  head.tool_name,
        });
        // badge = 队列中除 HEAD 外的其余 query 数量
        liveSx.setPendingQueryDepth(storeKey, Math.max(0, queries.length - 1));
      }
    }

    if (msg.type === 'reply') {
      const reply = msg as PSReply & {
        ps_id?: string;
        widget_id: string;
        request_id?: string;
        content?: string;
        done?: boolean;
        cancelled?: boolean;
        chunk_type?: 'text_delta' | 'tool_call_start' | 'tool_call_end' | 'thinking_delta' | 'round_separator' | 'fail';
        round?: number;
        full_content?: string;
        seq?: number;
        tool_call_name?: string;
        tool_call_id?: string;
        error_code?: string;
        error_message?: string;
        subagent_id?: string;
      };
      const widgetId = reply.widget_id;
      if (!widgetId) return;

      // ── 🆕 Agent 路由：chunk 统一写入 agentStreamStore ──────────
      // 主 Agent 用 '_main' key，子 Agent 用实际 subagent_id。
      // 子 Agent 写完后 return（不污染 liveStore 聊天消息列表）；
      // 主 Agent 继续走下面的 liveStore 路径以保持 chat log 显示。
      const agentS = useAgentStreamStore.getState();
      const psId = reply.ps_id ?? '';
      const requestId = reply.request_id ?? widgetId;
      const agentId = reply.subagent_id || '_main';
      agentS.appendChunk(psId, widgetId, requestId, agentId, {
        seq: reply.seq ?? 0,
        chunk_type: reply.chunk_type,
        content: reply.content,
        done: reply.done,
        tool_call_name: reply.tool_call_name,
        tool_call_id: reply.tool_call_id,
        round: reply.round,
        error_code: reply.error_code,
        error_message: reply.error_message,
      });
      if (reply.done || reply.cancelled) {
        agentS.endStream(psId, widgetId, requestId, agentId);
      }

      // 子 Agent 不继续走 liveStore 路径（不污染聊天消息列表）
      if (reply.subagent_id) {
        return;
      }

      // Composite key — use ps_id from the reply, not any local field
      const storeKey = `${reply.ps_id ?? ''}:${widgetId}`;

      // 追踪 seq 和 request→widget 映射（用于重连 resume_streams）
      if (reply.seq !== undefined) {
        this._lastSeqByRequest.set(requestId, reply.seq);
        this._requestWidget.set(requestId, widgetId);
      }

      // ── 处理工具调用 chunk ──────────────────────────────────────────
      if (reply.chunk_type === 'tool_call_start' && reply.tool_call_name && reply.tool_call_id) {
        liveS.addToolCall(storeKey, requestId, {
          id: reply.tool_call_id,
          name: reply.tool_call_name,
          status: 'running',
        });
        // 确保 assistant bubble 存在（如果还没有文本 chunk 则创建一个）
        const existing = (liveS.messages[storeKey] ?? []).find((m) => m.requestId === requestId);
        if (!existing) {
          liveS.addMessage(storeKey, {
            id: this._newMsgId(),
            role: 'assistant',
            text: '',
            requestId,
            streaming: true,
          });
        }
        return;
      }

      if (reply.chunk_type === 'tool_call_end' && reply.tool_call_id) {
        liveS.updateToolCall(storeKey, requestId, reply.tool_call_id, { status: 'done' });
        return;
      }

      // ── 思考内容 chunk → 追加到 assistant bubble ────────────────────
      if (reply.chunk_type === 'thinking_delta') {
        const existing = (liveS.messages[storeKey] ?? []).find((m) => m.requestId === requestId);
        if (existing) {
          liveS.patchMessage(storeKey, requestId, {
            text: existing.text + (reply.content ?? ''),
          });
        } else {
          liveS.addMessage(storeKey, {
            id: this._newMsgId(),
            role: 'assistant',
            text: reply.content ?? '',
            requestId,
            streaming: true,
          });
        }
        return;
      }

      // ── 轮次分隔符 → 追加到 assistant bubble ───────────────────────
      if (reply.chunk_type === 'round_separator') {
        const existing = (liveS.messages[storeKey] ?? []).find((m) => m.requestId === requestId);
        const sepText = '\n\n' + (reply.content ?? '') + '\n\n';
        if (existing) {
          liveS.patchMessage(storeKey, requestId, {
            text: existing.text + sepText,
            currentRound: reply.round,
          });
        } else {
          liveS.addMessage(storeKey, {
            id: this._newMsgId(),
            role: 'assistant',
            text: sepText,
            requestId,
            streaming: true,
            currentRound: reply.round,
          });
        }
        return;
      }

      // ── 🆕 处理文本 / 完成 / 取消 / fail ────────────────────────────
      if (reply.done || reply.cancelled) {
        const existing = (liveS.messages[storeKey] ?? []).find((m) => m.requestId === requestId);
        if (existing) {
          // 不重复拼接 content（流内容已通过 chunk 推送），仅标记完成
          liveS.patchMessage(storeKey, requestId, {
            streaming: false,
            ...(reply.full_content ? { fullContent: reply.full_content, displayText: reply.full_content } : {}),
          });
        } else if (reply.full_content && !reply.cancelled) {
          // Single-shot (non-streaming) reply — create and finalize in one go
          liveS.addMessage(storeKey, {
            id: this._newMsgId(),
            role: 'assistant',
            text: reply.full_content,
            displayText: reply.full_content,
            requestId,
            streaming: false,
          });
        }

        // 🆕 fail chunk：额外追加 error 气泡（done:True 会触发 streaming:false）
        if (reply.chunk_type === 'fail') {
          liveS.addMessage(storeKey, {
            id: this._newMsgId(),
            role: 'error',
            text: reply.error_message || reply.content || 'Stream failed',
            requestId,
          });
        }

        // 清理 seq 追踪
        this._lastSeqByRequest.delete(requestId);
        this._requestWidget.delete(requestId);
      } else {
        // Streaming chunk — append to existing bubble or start a new one
        const existing = (liveS.messages[storeKey] ?? []).find((m) => m.requestId === requestId);
        if (existing) {
          liveS.patchMessage(storeKey, requestId, { text: existing.text + (reply.content ?? '') });
        } else {
          liveS.addMessage(storeKey, {
            id: this._newMsgId(),
            role: 'assistant',
            text: reply.content ?? '',
            requestId,
            streaming: true,
          });
        }
      }
    }

    // 4. Write error messages to store so they persist across drawer close/reopen.
    //    Backend sends { type: "error", content: "...", widget_id?, request_id?, ps_id? }
    if (msg.type === 'error') {
      const err = msg as PSReply & {
        ps_id?: string;
        widget_id?: string;
        request_id?: string;
        content?: string;
      };
      const widgetId = err.widget_id;
      if (!widgetId) return;
      const storeKey = `${err.ps_id ?? ''}:${widgetId}`;
      const requestId = err.request_id ?? widgetId;
      // 🆕 终止该 request_id 的等待状态
      liveS.patchMessage(storeKey, requestId, { streaming: false });
      liveS.addMessage(storeKey, {
        id: this._newMsgId(),
        role: 'error',
        text: err.content ?? 'Unknown error',
        requestId,
      });
    }
  }

  private _scheduleReconnect(): void {
    const delay = RECONNECT_DELAYS[Math.min(this.retryCount, RECONNECT_DELAYS.length - 1)];
    this.retryCount++;
    this.retryTimer = setTimeout(() => {
      if (!this.intentionallyClosed) {
        this._connect();
      }
    }, delay);
  }

  private _clearRetryTimer(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }
}

/** Singleton bridge — import this anywhere in the app. */
export const bridge = new WSBridge();
