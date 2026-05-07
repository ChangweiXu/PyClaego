/**
 * CronEditBubble — floating card for creating/editing a single WidgetCronTrigger.
 *
 * Props:
 *   cron     - initial values (null when creating a new trigger)
 *   isNew    - true when creating; id is auto-generated
 *   onSave   - called with the final trigger value
 *   onClose  - called on Close or backdrop click
 */

import { useState, useEffect } from 'react';
import type { WidgetCronTrigger } from '../api';

interface Props {
  cron: WidgetCronTrigger | null;
  isNew: boolean;
  onSave: (cron: WidgetCronTrigger) => void;
  onClose: () => void;
}

function genId(): string {
  return `cr_${Date.now().toString(36)}`;
}

export default function CronEditBubble({ cron, isNew, onSave, onClose }: Props) {
  const [id] = useState<string>(() => cron?.id ?? genId());
  const [prompt, setPrompt] = useState(cron?.prompt ?? '');
  const [schedule, setSchedule] = useState(cron?.schedule ?? '');
  const [intervalSec, setIntervalSec] = useState<string>(
    cron?.interval_seconds != null ? String(cron.interval_seconds) : '',
  );
  const [enabled, setEnabled] = useState<boolean>(cron?.enabled ?? true);
  const [timezone, setTimezone] = useState(cron?.timezone ?? '');
  const [paramsJson, setParamsJson] = useState(
    cron?.params && Object.keys(cron.params).length > 0
      ? JSON.stringify(cron.params, null, 2)
      : '',
  );
  const [paramsError, setParamsError] = useState('');

  const original = JSON.stringify({
    prompt: cron?.prompt ?? '',
    schedule: cron?.schedule ?? '',
    interval_seconds: cron?.interval_seconds != null ? String(cron.interval_seconds) : '',
    enabled: cron?.enabled ?? true,
    timezone: cron?.timezone ?? '',
    params: cron?.params && Object.keys(cron.params).length > 0
      ? JSON.stringify(cron.params, null, 2)
      : '',
  });

  const current = JSON.stringify({ prompt, schedule, interval_seconds: intervalSec, enabled, timezone, params: paramsJson });
  const isDirty = isNew ? prompt.trim().length > 0 : current !== original;
  const canSave = isDirty && prompt.trim().length > 0 && paramsError === '';

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  const validateParams = (v: string) => {
    if (!v.trim()) { setParamsError(''); return; }
    try { JSON.parse(v); setParamsError(''); }
    catch { setParamsError('Invalid JSON'); }
  };

  const handleSave = () => {
    if (!canSave) return;
    let params: Record<string, unknown> = {};
    if (paramsJson.trim()) {
      try { params = JSON.parse(paramsJson); }
      catch { setParamsError('Invalid JSON'); return; }
    }
    const result: WidgetCronTrigger = {
      id,
      prompt: prompt.trim(),
      enabled,
    };
    if (schedule.trim()) result.schedule = schedule.trim();
    if (intervalSec.trim()) result.interval_seconds = parseInt(intervalSec, 10);
    if (timezone.trim()) result.timezone = timezone.trim();
    if (Object.keys(params).length > 0) result.params = params;
    onSave(result);
  };

  return (
    <>
      <div className="cron-bubble-backdrop" onClick={onClose} />
      <div className="cron-edit-bubble" role="dialog" aria-label="Edit cron trigger">
        <div className="cron-bubble-header">
          <span className="cron-bubble-title">{isNew ? 'New Cron Trigger' : `Edit · ${id}`}</span>
        </div>

        <div className="cron-bubble-body">
          <label className="cron-field">
            <span className="cron-field-label">Prompt <span className="cron-required">*</span></span>
            <textarea
              className="cron-field-textarea"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Message to send to the widget…"
              rows={3}
            />
          </label>

          <div className="cron-field-row">
            <label className="cron-field cron-field--half">
              <span className="cron-field-label">Schedule (cron)</span>
              <input
                className="cron-field-input"
                type="text"
                value={schedule}
                onChange={(e) => setSchedule(e.target.value)}
                placeholder="0 8 * * *"
              />
            </label>
            <label className="cron-field cron-field--half">
              <span className="cron-field-label">Interval (seconds)</span>
              <input
                className="cron-field-input"
                type="number"
                min={1}
                value={intervalSec}
                onChange={(e) => setIntervalSec(e.target.value)}
                placeholder="e.g. 300"
              />
            </label>
          </div>

          <label className="cron-field">
            <span className="cron-field-label">Timezone</span>
            <input
              className="cron-field-input"
              type="text"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              placeholder="e.g. Asia/Shanghai"
            />
          </label>

          <label className="cron-field">
            <span className="cron-field-label">
              Params (JSON object)
              {paramsError && <span className="cron-field-error"> — {paramsError}</span>}
            </span>
            <textarea
              className={`cron-field-textarea cron-field-mono${paramsError ? ' cron-field-textarea--error' : ''}`}
              value={paramsJson}
              onChange={(e) => { setParamsJson(e.target.value); validateParams(e.target.value); }}
              placeholder={'{\n  "key": "value"\n}'}
              rows={3}
            />
          </label>

          <label className="cron-field cron-field--checkbox">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span className="cron-field-label">Enabled</span>
          </label>
        </div>

        <div className="cron-bubble-footer">
          <button className="cron-close-btn" onClick={onClose}>Close</button>
          <button
            className="cron-save-btn"
            onClick={handleSave}
            disabled={!canSave}
          >
            Save
          </button>
        </div>
      </div>
    </>
  );
}
