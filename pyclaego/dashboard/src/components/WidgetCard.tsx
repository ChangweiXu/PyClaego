import type { WidgetSummary } from '../api';

interface Props {
  widget: WidgetSummary;
  highlight: Record<string, unknown>;
  index: number;
  busy: boolean; // live WS busy — overrides highlight.status
  onClick: () => void;
}

const VARIANTS = ['variant-dark', 'variant-mint', 'variant-white', 'variant-lav'] as const;

function statusFrom(h: Record<string, unknown>, busy: boolean): 'idle' | 'working' | 'error' {
  if (busy) return 'working';
  const s = h['status'];
  if (s === 'working' || s === 'busy') return 'working';
  if (s === 'error') return 'error';
  return 'idle';
}

export default function WidgetCard({ widget, highlight, index, busy, onClick }: Props) {
  const variant = VARIANTS[index % VARIANTS.length];
  const status = statusFrom(highlight, busy);

  const agentType  = highlight['agent_type']  as string | undefined;
  const contextType = highlight['context_type'] as string | undefined;
  const llmId      = highlight['llm']           as string | undefined;
  const msgCount   = highlight['msg_count']    as number | undefined;
  const question   = highlight['current_question'] as string | undefined;

  const chips: { label: string }[] = [];
  if (llmId)      chips.push({ label: llmId });
  if (agentType)  chips.push({ label: agentType });
  if (contextType) chips.push({ label: contextType });
  if (msgCount !== undefined) chips.push({ label: `${msgCount} msgs` });

  return (
    <div
      className={`widget-card ${variant}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && onClick()}
      aria-label={`Open widget ${widget.title || widget.widget_id}`}
    >
      <div className="card-top">
        <div>
          <div className="card-title">{widget.title || widget.widget_id}</div>
          <div className="card-meta">{widget.widget_class} · {widget.widget_id}</div>
        </div>
        <div
          className={`status-dot ${status}`}
          title={status}
        />
      </div>

      {question && status === 'working' && (
        <div className="card-question">"{question}"</div>
      )}

      {chips.length > 0 && (
        <div className="card-chips">
          {chips.map((c) => (
            <span key={c.label} className="card-badge">{c.label}</span>
          ))}
        </div>
      )}
    </div>
  );
}
