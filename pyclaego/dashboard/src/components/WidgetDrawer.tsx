/**
 * WidgetDrawer — schema-driven slide-in panel.
 *
 * Fetches the ViewSchema via useWidgetView() and renders it with
 * SchemaRenderer. The drawer no longer contains any chat/task logic —
 * that lives in ChatRenderer which is mounted when the schema contains
 * a chat_log node.
 */

import { useEffect } from 'react';
import { useWidgetView } from '../queries/widgets';
import { SchemaRenderer } from '../renderers/SchemaRenderer';
import { bridge } from '../ws/bridge';
import { api } from '../api';

interface Props {
  psId: string;
  widgetId: string;
  widgetTitle: string;
  widgetClass: string;
  onClose: () => void;
}

export default function WidgetDrawer({
  psId,
  widgetId,
  widgetTitle,
  widgetClass,
  onClose,
}: Props) {
  const { data: view, isLoading, error } = useWidgetView(psId, widgetId);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  const handleCommand = async (command: string, args?: Record<string, unknown>) => {
    if (command === 'stop') {
      bridge.send({ type: 'control', action: 'stop', ps_id: psId, widget_id: widgetId });
      return;
    }
    try {
      await api.sendCommand(psId, widgetId, command, args ?? {});
    } catch (e) {
      console.error('[WidgetDrawer] command error:', e);
    }
  };

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer" role="dialog" aria-label={`Widget: ${widgetTitle}`}>
        <div className="drawer-header">
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3>{widgetTitle || widgetId}</h3>
            <div className="meta">{widgetClass} · {widgetId}</div>
          </div>
          <button
            className="drawer-newwin"
            onClick={() => window.open(`/dashboard/chat/${psId}/${widgetId}`, '_blank')}
            aria-label="Open in new window"
            title="Open in new window"
          >⧉</button>
          <button className="drawer-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className={`drawer-body schema-body${view?.type === 'chat_log' ? ' schema-body--chat' : ''}`}>
          {isLoading && <div className="drawer-loading">Loading view…</div>}
          {error && <div className="drawer-error">Failed to load view: {String(error)}</div>}
          {view && (
            <SchemaRenderer
              schema={view}
              ctx={{ psId, widgetId, onCommand: handleCommand }}
            />
          )}
        </div>
      </div>
    </>
  );
}
