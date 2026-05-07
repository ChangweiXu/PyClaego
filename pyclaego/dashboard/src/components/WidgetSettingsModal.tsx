import { useState, useEffect, useCallback } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { json } from '@codemirror/lang-json';
import { EditorView } from '@codemirror/view';
import { api } from '../api';
import { queryClient } from '../queries/client';

const SECTIONS = ['agent', 'context', 'ps_metadata', 'context_subagents'] as const;
type SectionKey = (typeof SECTIONS)[number];

interface Props {
  open: boolean;
  psId: string;
  widgetId: string;
  onClose: () => void;
}

function toJsonText(v: unknown): string {
  if (!v || (typeof v === 'object' && Object.keys(v as object).length === 0)) return '';
  try { return JSON.stringify(v, null, 2); } catch { return ''; }
}

function tryParseJson(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  const t = text.trim();
  if (!t) return { ok: true, value: undefined };
  try { return { ok: true, value: JSON.parse(t) }; }
  catch (e) { return { ok: false, error: (e as Error).message }; }
}

export function WidgetSettingsModal({ open, psId, widgetId, onClose }: Props) {
  const [title, setTitle] = useState('');
  const [sectionTexts, setSectionTexts] = useState<Record<SectionKey, string>>({
    agent: '', context: '', ps_metadata: '', context_subagents: '',
  });
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [saving, setSaving] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);

  // Track originals for dirty detection
  const [originalTitle, setOriginalTitle] = useState('');
  const [originalTexts, setOriginalTexts] = useState<Record<SectionKey, string>>({
    agent: '', context: '', ps_metadata: '', context_subagents: '',
  });

  const dirty = (
    title !== originalTitle ||
    SECTIONS.some((k) => sectionTexts[k] !== originalTexts[k])
  );

  // Load data when modal opens
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const info = await api.getWidget(psId, widgetId);
        if (cancelled) return;
        const t = (info.manifest as Record<string, unknown>)?.title as string ?? '';
        setTitle(t);
        setOriginalTitle(t);

        const wc = info.widget_config ?? {};
        const texts: Record<SectionKey, string> = {} as Record<SectionKey, string>;
        for (const k of SECTIONS) {
          texts[k] = toJsonText((wc as Record<string, unknown>)[k]);
        }
        setSectionTexts(texts);
        setOriginalTexts({ ...texts });
        setErrors({});
        setConfirmClose(false);
      } catch {
        // silently fail — modal stays empty
      }
    })();
    return () => { cancelled = true; };
  }, [open, psId, widgetId]);

  const handleClose = useCallback(() => {
    if (dirty) {
      setConfirmClose(true);
    } else {
      onClose();
    }
  }, [dirty, onClose]);

  const handleSave = useCallback(async () => {
    // Validate all sections
    const newErrors: Record<string, string | null> = {};
    const parsed: Record<string, unknown> = {};
    let hasError = false;

    for (const k of SECTIONS) {
      const r = tryParseJson(sectionTexts[k]);
      if (!r.ok) {
        newErrors[k] = r.error;
        hasError = true;
      } else {
        newErrors[k] = null;
        if (r.value !== undefined) {
          parsed[k] = r.value;
        }
      }
    }
    setErrors(newErrors);
    if (hasError) return;

    setSaving(true);
    try {
      const promises: Promise<unknown>[] = [];

      // Build config — only include non-empty sections
      const config: Record<string, unknown> = {};
      for (const k of SECTIONS) {
        if (k in parsed) {
          config[k] = parsed[k];
        }
      }
      if (Object.keys(config).length > 0) {
        promises.push(api.updateWidgetConfig(psId, widgetId, config));
      }

      // Update title if changed
      if (title !== originalTitle) {
        promises.push(api.updateWidgetManifest(psId, widgetId, { title }));
      }

      await Promise.all(promises);

      // Invalidate highlight so llm/agent display refreshes
      queryClient.invalidateQueries({ queryKey: ['highlight', psId, widgetId] });

      onClose();
    } catch (e) {
      setErrors((prev) => ({ ...prev, _save: String(e) }));
    } finally {
      setSaving(false);
    }
  }, [sectionTexts, title, originalTitle, psId, widgetId, onClose]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div className="modal widget-settings-modal" onClick={(e) => e.stopPropagation()}>
        <div className="json-modal-header">
          <h2>Widget Settings</h2>
          <button className="btn-icon" onClick={handleClose} title="Close">✕</button>
        </div>

        <div className="settings-body">
          {/* Title */}
          <div className="settings-title-row">
            <span className="settings-title-label">Title</span>
            <input
              className="settings-title-input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Widget display title"
            />
          </div>

          {/* Config sections */}
          {SECTIONS.map((key) => (
            <div className="settings-section" key={key}>
              <div className="settings-section-label">{key}</div>
              <div className={`settings-cm-editor${errors[key] ? ' has-error' : ''}`}>
                <CodeMirror
                  value={sectionTexts[key]}
                  onChange={(val) => {
                    setSectionTexts((prev) => ({ ...prev, [key]: val }));
                    if (errors[key]) setErrors((prev) => ({ ...prev, [key]: null }));
                  }}
                  extensions={[json(), EditorView.lineWrapping]}
                  placeholder={`{ }`}
                  height="100px"
                  basicSetup={{
                    lineNumbers: false,
                    foldGutter: false,
                    dropCursor: false,
                    allowMultipleSelections: false,
                    indentOnInput: true,
                    searchKeymap: false,
                  }}
                />
              </div>
              {errors[key] && (
                <div className="settings-error">Invalid JSON: {errors[key]}</div>
              )}
            </div>
          ))}

          {errors._save && (
            <div className="settings-error" style={{ marginBottom: '0.5rem' }}>
              Save failed: {errors._save}
            </div>
          )}
        </div>

        <div className="settings-actions">
          <button className="btn" onClick={handleClose} disabled={saving}>Close</button>
          <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>

        {/* Confirm close overlay */}
        {confirmClose && (
          <div className="settings-confirm-overlay">
            <div className="settings-confirm-box">
              <p style={{ fontWeight: 600 }}>Unsaved changes</p>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '0.3rem' }}>
                Discard changes?
              </p>
              <div className="settings-confirm-actions">
                <button className="btn btn-sm" onClick={() => setConfirmClose(false)}>Cancel</button>
                <button
                  className="btn btn-sm btn-danger"
                  onClick={() => { setConfirmClose(false); onClose(); }}
                >Discard</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
