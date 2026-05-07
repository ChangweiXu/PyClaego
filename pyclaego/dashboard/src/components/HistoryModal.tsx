/**
 * HistoryModal — centered pop-up dialog showing the full widget history
 * from history_manager.load_all() as pretty-printed JSON.
 *
 * Style matches JsonModal (Task2 json-modal) for visual consistency.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { HighlightedJson, toJsonString } from '../primitives/HighlightedJson';

interface HistoryModalProps {
  open: boolean;
  psId: string;
  widgetId: string;
  onClose: () => void;
}

export default function HistoryModal({ open, psId, widgetId, onClose }: HistoryModalProps) {
  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal json-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="json-modal-header">
          <h2 style={{ margin: 0, flex: 1 }}>History — {widgetId}</h2>
          <button className="artifact-view-btn" onClick={onClose}>close</button>
        </div>
        <HistoryContent psId={psId} widgetId={widgetId} />
      </div>
    </div>
  );
}

function HistoryContent({ psId, widgetId }: { psId: string; widgetId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['widget-history', psId, widgetId],
    queryFn: () => api.getWidgetHistory(psId, widgetId),
    staleTime: 5_000,
  });

  if (isLoading) {
    return (
      <div className="json-view" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 200 }}>
        Loading history…
      </div>
    );
  }

  if (error) {
    return (
      <div className="json-view" style={{ color: '#fca5a5', minHeight: 200 }}>
        Failed to load history: {String(error)}
      </div>
    );
  }

  const history = data?.history ?? [];
  const jsonString = toJsonString(history);

  return <HighlightedJson text={jsonString} />;
}
