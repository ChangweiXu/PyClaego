/**
 * MermaidBlock — custom react-markdown code renderer for Mermaid diagrams.
 *
 * Detects ```mermaid code blocks and renders them as SVG using mermaid.run().
 * Falls back to a plain <pre><code> block for non-mermaid code.
 */

import { useEffect, useRef, useState, type ReactElement } from 'react';

interface MermaidBlockProps {
  className?: string;
  children?: React.ReactNode;
}

export function MermaidBlock({ className, children }: MermaidBlockProps): ReactElement {
  const language = className?.replace('language-', '') ?? '';
  const code = String(children ?? '').replace(/\n$/, '');

  // Non-mermaid code blocks: render as plain <pre><code>
  if (language !== 'mermaid') {
    return (
      <pre>
        <code className={className}>{code}</code>
      </pre>
    );
  }

  return <MermaidDiagram code={code} />;
}

/** Renders a single Mermaid diagram as SVG. */
function MermaidDiagram({ code }: { code: string }): ReactElement {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const idRef = useRef(`mermaid-${Math.random().toString(36).slice(2, 10)}`);

  useEffect(() => {
    let cancelled = false;

    import('mermaid').then(async (mermaid) => {
      if (cancelled) return;

      mermaid.default.initialize({
        startOnLoad: false,
        theme: 'default',
        securityLevel: 'loose',
      });

      try {
        const { svg: rendered } = await mermaid.default.render(idRef.current, code);
        if (!cancelled) {
          setSvg(rendered);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(String(err));
          setSvg(null);
        }
      }
    }).catch((err) => {
      if (!cancelled) setError(String(err));
    });

    return () => { cancelled = true; };
  }, [code]);

  if (error) {
    return (
      <div className="mermaid-error" title={error}>
        <pre><code className="language-mermaid">{code}</code></pre>
        <span className="mermaid-error-msg">⚠️ Mermaid render failed</span>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className="mermaid-loading" ref={containerRef}>
        <pre><code className="language-mermaid">{code}</code></pre>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="mermaid-container"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
