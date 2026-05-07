// PS-aware chat WebSocket helper. 协议与 PSGateway 一致。

export type PSMsg =
  | {
      type: 'open';
      ps_id: string;
      user_id?: string;
      /** 重连时携带：widget_id → {request_id: last_seq | {subagent_id: last_seq}} */
      resume_streams?: Record<string, Record<string, number | Record<string, number>>>;
    }
  | { type: 'close'; ps_id: string }
  | {
      type: 'chat';
      ps_id: string;
      widget_id: string;
      content: string;
      /** Multimodal content parts (text + image_url). When present, the backend
       *  passes them through to the widget's process_message call. */
      content_parts?: Array<
        | { type: 'text'; text: string }
        | { type: 'image_url'; image_url: { url: string } }
      >;
      request_id?: string;
      source?: 'chat' | 'cron';
      user_id?: string;
    }
  | {
      type: 'control';
      action: string;
      ps_id: string;
      widget_id: string;
      request_id?: string;
    };

export type PSReply =
  | {
      type: 'ack';
      action?: string;
      ps_id?: string;
      widget_ids?: string[];
      /** 重连回放结果：widget_id → {request_id: replayed_count} */
      replayed?: Record<string, Record<string, number>>;
      /** 当前活跃的流：{widget_id, request_id, subagent_id?, seq, finished} */
      active_streams?: Array<{ widget_id: string; request_id: string; subagent_id?: string; seq: number; finished: boolean }>;
      [k: string]: unknown;
    }
  | {
      type: 'reply';
      ps_id: string;
      widget_id: string;
      content: string;
      request_id?: string;
      done?: boolean;
      cancelled?: boolean;
      /** 流 chunk 类型 */
      chunk_type?: 'text_delta' | 'tool_call_start' | 'tool_call_end' | 'thinking_delta' | 'round_separator' | 'fail';
      /** 子 Agent 标识（存在则表示该 chunk 属于指定子 Agent） */
      subagent_id?: string;
      /** 流序列号（用于重连时定位回放起点） */
      seq?: number;
      /** 轮次号（round_separator 时有效） */
      round?: number;
      /** tool_call_start/end 时的工具名称 */
      tool_call_name?: string;
      /** tool_call_start/end 时的工具调用 ID */
      tool_call_id?: string;
      /** 完整响应内容（done:True 时有效，用于 sidebar 展示，非流式重复） */
      full_content?: string;
      /** 错误码（chunk_type="fail" 时有效） */
      error_code?: string;
      /** 错误描述（chunk_type="fail" 时有效） */
      error_message?: string;
      [k: string]: unknown;
    }
  | { type: 'error'; content: string; [k: string]: unknown }
  | { type: 'event'; [k: string]: unknown };

export class PSChatClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<(m: PSReply) => void>();
  private opened: Promise<void>;
  private resolveOpened!: () => void;

  constructor(private url: string = '/ws/v2/chat') {
    this.opened = new Promise<void>((res) => (this.resolveOpened = res));
  }

  async connect(): Promise<void> {
    if (this.ws) return this.opened;
    const wsUrl =
      this.url.startsWith('ws://') || this.url.startsWith('wss://')
        ? this.url
        : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${this.url}`;
    this.ws = new WebSocket(wsUrl);
    this.ws.onopen = () => this.resolveOpened();
    this.ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as PSReply;
        this.listeners.forEach((l) => l(data));
      } catch {
        /* ignore */
      }
    };
    this.ws.onclose = () => {
      this.ws = null;
    };
    return this.opened;
  }

  send(msg: PSMsg): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error('PSChatClient not connected');
    }
    this.ws.send(JSON.stringify(msg));
  }

  on(listener: (m: PSReply) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close(): void {
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }
}
