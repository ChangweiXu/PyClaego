import { useEffect, useRef } from 'react';
import Cytoscape from 'cytoscape';
import type { GraphData } from './notesRpc';
import './notes.css';

interface Props {
  data: GraphData;
  onNodeClick: (docId: string) => void;
}

export default function GraphView({ data, onNodeClick }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Cytoscape.Core | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    cyRef.current = Cytoscape({
      container: containerRef.current,
      elements: [
        ...data.nodes.map((n) => ({
          data: { id: n.id, label: n.label, rel_path: n.rel_path, stub: n.stub ?? false },
        })),
        ...data.edges.map((e) => ({
          data: { id: `${e.source}→${e.target}`, source: e.source, target: e.target },
        })),
      ],
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'text-valign': 'bottom',
            'text-halign': 'center',
            'font-size': '11px',
            width: 28,
            height: 28,
            'background-color': '#4f8ef7',
            color: '#333',
            'text-max-width': '120px',
            'text-wrap': 'ellipsis',
          },
        },
        {
          selector: 'node[?stub]',
          style: { 'background-color': '#ccc', 'border-style': 'dashed', 'border-width': 2 },
        },
        {
          selector: 'edge',
          style: {
            width: 1.5,
            'line-color': '#aaa',
            'target-arrow-color': '#aaa',
            'target-arrow-shape': 'triangle',
            'curve-style': 'unbundled-bezier',
          },
        },
        {
          selector: ':selected',
          style: { 'background-color': '#f06', 'line-color': '#f06', 'target-arrow-color': '#f06' },
        },
      ],
      layout: { name: 'cose', animate: false } as any,
      minZoom: 0.3,
      maxZoom: 3,
    });

    cyRef.current.on('tap', 'node', (e) => {
      const docId: string = e.target.data('id');
      if (docId && !e.target.data('stub')) onNodeClick(docId);
    });

    return () => {
      cyRef.current?.destroy();
      cyRef.current = null;
    };
  }, [data]);

  return <div ref={containerRef} className="notes-graph-container" />;
}
