import { useState, useEffect } from 'react';
import { api, type WidgetClassInfo } from '../api';

interface Props {
  psId: string;
  classes: WidgetClassInfo[];
  onClose: () => void;
  onCreated: () => void;
}

function jsonPretty(v: unknown): string {
  if (!v || (typeof v === 'object' && Object.keys(v as object).length === 0)) return '';
  return JSON.stringify(v, null, 2);
}

function tryParse(s: string): Record<string, unknown> | undefined {
  const t = s.trim();
  if (!t) return undefined;
  try { return JSON.parse(t) as Record<string, unknown>; } catch { return undefined; }
}

export default function AddWidgetModal({ psId, classes, onClose, onCreated }: Props) {
  const [selected, setSelected] = useState<WidgetClassInfo | null>(null);
  const [widgetId, setWidgetId] = useState('');
  const [title, setTitle]       = useState('');
  const [llmId, setLlmId]       = useState('');
  const [docRoot, setDocRoot]   = useState('');
  const [agentJson, setAgentJson]     = useState('');
  const [contextJson, setContextJson] = useState('');
  const [jsonError, setJsonError]     = useState<string | null>(null);
  const [busy, setBusy]         = useState(false);
  const [llmProviders, setLlmProviders]       = useState<string[] | null>(null);
  const [defaultProvider, setDefaultProvider] = useState('');
  const [llmProvidersError, setLlmProvidersError] = useState(false);

  useEffect(() => {
    api.getLLMProviders()
      .then((data) => {
        setLlmProviders(data.providers);
        setDefaultProvider(data.default_provider);
      })
      .catch(() => setLlmProvidersError(true));
  }, []);

  function selectClass(c: WidgetClassInfo) {
    setSelected(c);
    setWidgetId(`w_${c.class_id}_${Date.now().toString(36).slice(-4)}`);
    setTitle(c.title);
    setDocRoot(c.class_id === 'notes' ? '<widget_root>/notes' : '');
    const defaults = c.defaults as Record<string, unknown> | undefined;
    const agentDef   = defaults?.['agent']   as Record<string, unknown> | undefined;
    const contextDef = defaults?.['context'] as Record<string, unknown> | undefined;
    setLlmId((agentDef?.['llm'] as string | undefined) ?? defaultProvider);
    setAgentJson(jsonPretty(agentDef));
    setContextJson(jsonPretty(contextDef));
    setJsonError(null);
  }

  function buildConfig(): Record<string, unknown> | null {
    const agentParsed   = agentJson.trim()   ? tryParse(agentJson)   : {};
    const contextParsed = contextJson.trim() ? tryParse(contextJson) : {};
    if (agentJson.trim() && agentParsed === undefined) {
      setJsonError('agent: invalid JSON');
      return null;
    }
    if (contextJson.trim() && contextParsed === undefined) {
      setJsonError('context: invalid JSON');
      return null;
    }
    setJsonError(null);
    const agent: Record<string, unknown> = { ...(agentParsed ?? {}) };
    if (llmId.trim()) agent['llm'] = llmId.trim();
    const cfg: Record<string, unknown> = {};
    if (Object.keys(agent).length)            cfg['agent']   = agent;
    if (contextParsed && Object.keys(contextParsed).length) cfg['context'] = contextParsed;
    if (selected?.class_id === 'notes' && docRoot.trim()) cfg['doc_root'] = docRoot.trim();
    return cfg;
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Add Widget</h2>
        {!selected ? (
          <div>
            <p style={{ marginBottom: '0.75rem', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              Pick a widget class:
            </p>
            <div style={{ display: 'grid', gap: '0.5rem' }}>
              {classes.filter((c) => c.class_id !== 'notes').map((c) => (
                <button
                  key={c.class_id}
                  className="btn"
                  style={{ textAlign: 'left', padding: '0.6rem 0.9rem' }}
                  onClick={() => selectClass(c)}
                >
                  <b>{c.title}</b> <span className="meta">({c.class_id})</span>
                  {c.description && <div className="meta" style={{ marginTop: '0.2rem' }}>{c.description}</div>}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
            <p style={{ margin: 0 }}>
              Class: <b>{selected.title}</b>{' '}
              <button className="btn btn-sm" onClick={() => setSelected(null)}>← change</button>
            </p>

            <ModalField label="widget_id">
              <input className="modal-input" value={widgetId} onChange={(e) => setWidgetId(e.target.value)} />
            </ModalField>

            <ModalField label="title">
              <input className="modal-input" value={title} onChange={(e) => setTitle(e.target.value)} />
            </ModalField>

            {selected.class_id === 'notes' && (
              <ModalField label="doc_root" hint="path to the notes vault directory">
                <input
                  className="modal-input"
                  value={docRoot}
                  onChange={(e) => setDocRoot(e.target.value)}
                  placeholder="<widget_root>/notes"
                />
              </ModalField>
            )}

            <hr style={{ border: 'none', borderTop: '1px solid #e9ecef', margin: '0.2rem 0' }} />
            <p style={{ margin: 0, fontWeight: 700, fontSize: '0.9rem' }}>Config</p>

            <ModalField label="llm" hint={llmProvidersError ? 'e.g. gpt-4o, claude-sonnet-4-5' : undefined}>
              {llmProvidersError ? (
                <input
                  className="modal-input"
                  value={llmId}
                  onChange={(e) => setLlmId(e.target.value)}
                  placeholder="leave blank to use server default"
                />
              ) : (
                <select
                  className="modal-input"
                  value={llmId}
                  onChange={(e) => setLlmId(e.target.value)}
                  disabled={llmProviders === null}
                >
                  <option value="">
                    {llmProviders === null ? 'Loading providers…' : '— use server default —'}
                  </option>
                  {(llmProviders ?? []).map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              )}
            </ModalField>

            <ModalField label="agent" hint="JSON — merged with llm_id above">
              <textarea
                className="modal-input modal-textarea"
                value={agentJson}
                onChange={(e) => { setAgentJson(e.target.value); setJsonError(null); }}
                placeholder={'{\n  "type": "llm_agent"\n}'}
                rows={4}
              />
            </ModalField>

            <ModalField label="context" hint="JSON">
              <textarea
                className="modal-input modal-textarea"
                value={contextJson}
                onChange={(e) => { setContextJson(e.target.value); setJsonError(null); }}
                placeholder={'{\n  "type": "memory_context"\n}'}
                rows={4}
              />
            </ModalField>

            {jsonError && (
              <p style={{ color: 'var(--danger)', fontSize: '0.8rem', margin: 0 }}>⚠ {jsonError}</p>
            )}

            <div style={{ display: 'flex', gap: '0.5rem', paddingTop: '0.25rem' }}>
              <button className="btn" onClick={onClose}>Cancel</button>
              <button
                className="btn btn-primary"
                disabled={busy || !widgetId.trim()}
                onClick={async () => {
                  const cfg = buildConfig();
                  if (cfg === null) return;
                  setBusy(true);
                  try {
                    await api.createWidget(psId, {
                      widget_id: widgetId.trim(),
                      widget_class: selected.class_id,
                      title,
                      config: cfg,
                    });
                    onCreated();
                  } catch (e) {
                    alert(String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ModalField({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
      <span style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-muted)' }}>
        {label}
        {hint && <span style={{ fontWeight: 400, marginLeft: '0.4rem' }}>{hint}</span>}
      </span>
      {children}
    </label>
  );
}
