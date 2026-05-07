/**
 * ArtifactViewer — inline blob viewer for a single task artifact.
 * Fetches on mount; JSON is pretty-printed; content truncated at 20k chars.
 */
import { useEffect, useState } from 'react';
import { api } from '../api';
import type { ArtifactRef } from '../api';

const MAX_CHARS = 20_000;

interface Props {
  taskId: string;
  artifact: ArtifactRef;
}

export default function ArtifactViewer({ taskId, artifact }: Props) {
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading');
  const [content, setContent] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    setState('loading');
    api.getTaskArtifactBlob(taskId, artifact.artifact_id)
      .then(({ text, mime }) => {
        let body = text;
        if (mime.includes('json')) {
          try { body = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ }
        }
        if (body.length > MAX_CHARS) body = body.slice(0, MAX_CHARS) + '\n…(truncated)';
        setContent(body);
        setState('ok');
      })
      .catch((e: unknown) => {
        setError(String(e));
        setState('error');
      });
  }, [taskId, artifact.artifact_id]);

  if (state === 'loading') return <div className="artifact-viewer-loading">Loading…</div>;
  if (state === 'error')   return <div className="artifact-viewer-error">Error: {error}</div>;

  return (
    <div className="artifact-viewer">
      <div className="artifact-viewer-meta">{artifact.name} · {artifact.mime}</div>
      <pre className="artifact-viewer-pre">{content}</pre>
    </div>
  );
}
