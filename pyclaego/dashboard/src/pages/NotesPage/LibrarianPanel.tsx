import { useState, useRef, useEffect, useCallback } from 'react';


interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

interface Props {
  psId: string;
  widgetId: string;
  onClose: () => void;
}

export default function LibrarianPanel({ psId, widgetId, onClose }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Scroll to bottom whenever messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;
    const userMsg: ChatMessage = { role: 'user', content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setLoading(true);
    try {
      // Pass only previous messages (not the one we just added) as history
      const history = [...messages, userMsg].slice(0, -1); // exclude the new user msg
      const base = '/api/v2';
      const resp = await fetch(
        `${base}/personal_spaces/${encodeURIComponent(psId)}/widgets/${encodeURIComponent(widgetId)}/notes/librarian/chat`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text, history }) },
      );
      if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
      const res = await resp.json() as { reply: string };
      setMessages((prev) => [...prev, { role: 'assistant', content: res.reply }]);
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `Error: ${String(e)}` },
      ]);
    } finally {
      setLoading(false);
      // Re-focus the input
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [input, loading, messages, psId, widgetId]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="notes-librarian-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="notes-librarian-window">
        <div className="notes-librarian-header">
          <span>🤖 Librarian</span>
          <button className="notes-btn-ghost" onClick={onClose}>✕</button>
        </div>

        <div className="notes-librarian-body">
          <div className="lib-messages">
            {messages.length === 0 && (
              <div className="lib-intro">
                <p>你好！我是 Librarian，你笔记库的 AI 助手。</p>
                <p>我可以帮你搜索、阅读、整理和创建笔记。</p>
                <div className="lib-chips">
                  {['帮我搜索最近的想法', '笔记库里有什么文件？', '创建一条今日待办'].map((q) => (
                    <button
                      key={q}
                      className="lib-chip"
                      onClick={() => { setInput(q); inputRef.current?.focus(); }}
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`lib-msg lib-msg-${m.role}`}>
                <div className="lib-msg-bubble">
                  {m.content.split('\n').map((line, j) => (
                    <span key={j}>
                      {line}
                      {j < m.content.split('\n').length - 1 && <br />}
                    </span>
                  ))}
                </div>
              </div>
            ))}

            {loading && (
              <div className="lib-msg lib-msg-assistant">
                <div className="lib-msg-bubble lib-typing">
                  <span /><span /><span />
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          <div className="lib-input-row">
            <textarea
              ref={inputRef}
              className="lib-input"
              placeholder="问 Librarian… (⌘↩ 发送)"
              value={input}
              rows={2}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={loading}
              autoFocus
            />
            <button
              className="lib-send-btn"
              onClick={send}
              disabled={loading || !input.trim()}
            >
              {loading ? '…' : '↑'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
