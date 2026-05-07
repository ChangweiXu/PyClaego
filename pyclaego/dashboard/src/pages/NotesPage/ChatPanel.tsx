/**
 * ChatPanel.tsx — Document chat panel for Notes page
 *
 * Provides a simple chat interface to talk with the document agent.
 * Reuses the existing WebSocket bridge and liveStore patterns from the main Chat module.
 */
import { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { bridge } from '../../ws/bridge'
import { useLiveStore, type ChatMessage } from '../../store/live'

interface Props {
  psId: string
  widgetId: string
  activeRelPath: string | null
}

function generateRequestId(): string {
  return `notes_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

export default function ChatPanel({ psId, widgetId, activeRelPath }: Props) {
  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const storeKey = `${psId}:${widgetId}`

  // Read messages from live store
  const messages = useLiveStore((s) => s.messages[storeKey] ?? [])
  const isTyping = useLiveStore((s) => s.busy[storeKey] ?? false)
  const addMessage = useLiveStore((s) => s.addMessage)
  const patchMessage = useLiveStore((s) => s.patchMessage)
  const setBusy = useLiveStore((s) => s.setBusy)

  // Ensure PS is opened in WS bridge
  useEffect(() => {
    bridge.ensurePSOpen(psId)
  }, [psId])

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Handle send message
  const handleSend = useCallback(() => {
    const text = input.trim()
    if (!text) return

    const requestId = generateRequestId()

    // Add user message immediately
    addMessage(storeKey, {
      id: `msg_${requestId}_user`,
      role: 'user',
      text,
      requestId,
    })

    // Set typing indicator
    setBusy(storeKey, true)

    // Send via WS bridge
    bridge.send({
      type: 'chat',
      ps_id: psId,
      widget_id: widgetId,
      content: text,
      request_id: requestId,
      source: 'chat',
    })

    setInput('')
  }, [input, psId, widgetId, storeKey, addMessage, setBusy])

  // Handle Enter to send (Cmd+Enter or Ctrl+Enter)
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      handleSend()
    }
  }, [handleSend])

  // Handle stop generation
  const handleStop = useCallback(() => {
    bridge.send({
      type: 'control',
      action: 'stop',
      ps_id: psId,
      widget_id: widgetId,
    })
    setBusy(storeKey, false)
  }, [psId, widgetId, storeKey, setBusy])

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 120)}px`
    }
  }, [input])

  return (
    <div className="notes-chat-panel">
      {/* Header */}
      <div className="notes-chat-header">
        <span className="notes-chat-title">💬 Document Agent</span>
        {activeRelPath && (
          <span className="notes-chat-context" title={activeRelPath}>
            📄 {activeRelPath.split('/').pop()}
          </span>
        )}
      </div>

      {/* Messages */}
      <div className="notes-chat-messages">
        {messages.length === 0 && (
          <div className="notes-chat-empty">
            <p>Start a conversation with the document agent.</p>
            <p className="notes-chat-hint">Ask questions about the current document or request edits.</p>
          </div>
        )}
        {messages.map((msg) => (
          <div key={msg.id} className={`notes-chat-bubble notes-chat-${msg.role}`}>
            <div className="notes-chat-bubble-content">
              {msg.role === 'user' ? (
                <pre className="notes-chat-user-text">{msg.text}</pre>
              ) : msg.role === 'error' ? (
                <div className="notes-chat-error-text">⚠ {msg.text}</div>
              ) : (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.text}</ReactMarkdown>
              )}
            </div>
            {msg.streaming && (
              <div className="notes-chat-typing">
                <span className="notes-chat-typing-dot" />
                <span className="notes-chat-typing-dot" />
                <span className="notes-chat-typing-dot" />
              </div>
            )}
          </div>
        ))}
        {isTyping && messages.filter((m) => m.role === 'assistant' && m.streaming).length === 0 && (
          <div className="notes-chat-bubble notes-chat-assistant">
            <div className="notes-chat-typing">
              <span className="notes-chat-typing-dot" />
              <span className="notes-chat-typing-dot" />
              <span className="notes-chat-typing-dot" />
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="notes-chat-input-area">
        <textarea
          ref={textareaRef}
          className="notes-chat-textarea"
          placeholder="Ask about the document…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
        />
        <div className="notes-chat-input-actions">
          {isTyping ? (
            <button className="notes-chat-btn notes-chat-btn-stop" onClick={handleStop} title="Stop generation">
              ■ Stop
            </button>
          ) : (
            <button
              className="notes-chat-btn notes-chat-btn-send"
              onClick={handleSend}
              disabled={!input.trim()}
              title="Send (Cmd+Enter)"
            >
              ↑ Send
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
