import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api, type WidgetClassInfo, type WidgetSummary } from '../api';
import { useWidgets, useWidgetHighlight } from '../queries/widgets';
import { bridge } from '../ws/bridge';
import AddWidgetModal from '../components/AddWidgetModal';
import WidgetCard from '../components/WidgetCard';
import WidgetDrawer from '../components/WidgetDrawer';

interface ActiveWidget {
  widget_id: string;
  widget_class: string;
  title: string;
}

export default function DashboardPage() {
  const { psId = '' } = useParams();
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: widgets = [], isLoading } = useWidgets(psId);
  const [classes, setClasses]   = useState<WidgetClassInfo[]>([]);
  const [showAdd, setShowAdd]   = useState(false);
  const [active, setActive]     = useState<ActiveWidget | null>(null);

  // Load widget classes for AddWidgetModal
  useEffect(() => {
    api.listWidgetClasses().then((r) => setClasses(r.widget_classes)).catch(console.error);
  }, []);

  // Register this PS with the persistent bridge (idempotent, no cleanup needed)
  useEffect(() => {
    if (!psId) return;
    bridge.ensurePSOpen(psId);
  }, [psId]);

  if (isLoading) return <div style={{ padding: '2rem', color: '#64748b' }}>Loading {psId}…</div>;

  return (
    <div>
      <div className="ps-page-header">
        <h2>{psId}</h2>
        <button className="btn-add-widget" onClick={() => setShowAdd(true)}>＋ Add Widget</button>
      </div>

      {widgets.length === 0 ? (
        <div style={{ color: '#64748b', fontSize: '0.9rem', padding: '2rem 0' }}>
          No widgets yet — click <b>＋ Add Widget</b> to create one.
        </div>
      ) : (
        <div className="bento-grid">
          {widgets.map((w: WidgetSummary, i: number) => (
            <WidgetCardWrapper
              key={w.widget_id}
              psId={psId}
              widget={w}
              index={i}
              onClick={() => {
                if (w.widget_class === 'notes') {
                  navigate(`/ps/${psId}/notes/${encodeURIComponent(w.widget_id)}`);
                } else {
                  setActive({ widget_id: w.widget_id, widget_class: w.widget_class, title: w.title });
                }
              }}
            />
          ))}
        </div>
      )}

      {active && (
        <WidgetDrawer
          psId={psId}
          widgetId={active.widget_id}
          widgetTitle={active.title}
          widgetClass={active.widget_class}
          onClose={() => setActive(null)}
        />
      )}

      {showAdd && (
        <AddWidgetModal
          psId={psId}
          classes={classes}
          onClose={() => setShowAdd(false)}
          onCreated={() => {
            setShowAdd(false);
            qc.invalidateQueries({ queryKey: ['widgets', psId] });
          }}
        />
      )}
    </div>
  );
}

/** Thin wrapper that reads the highlight from TanStack Query cache. */
function WidgetCardWrapper({
  psId,
  widget,
  index,
  onClick,
}: {
  psId: string;
  widget: WidgetSummary;
  index: number;
  onClick: () => void;
}) {
  const { data: highlight = {} } = useWidgetHighlight(psId, widget.widget_id);
  const busy = !!(highlight['busy'] || highlight['status'] === 'working');
  return (
    <WidgetCard
      widget={widget}
      highlight={highlight}
      index={index}
      busy={busy}
      onClick={onClick}
    />
  );
}
