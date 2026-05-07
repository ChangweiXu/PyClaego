import type { DocumentListSchema } from '../schema/types';

interface DocumentListProps extends DocumentListSchema {
  /** Resolve a doc_id to markdown text. Injected by renderer context. */
  resolveDoc?: (docId: string) => string | undefined;
}

export function DocumentList({ doc_ids, resolveDoc }: DocumentListProps) {
  if (!doc_ids.length) return <div className="p-doclist-empty">No documents.</div>;
  return (
    <div className="p-doclist">
      {doc_ids.map((id) => {
        const text = resolveDoc ? resolveDoc(id) : undefined;
        return (
          <div key={id} className="p-doclist-item">
            <div className="p-doclist-id">{id}</div>
            {text ? (
              <pre className="p-doclist-content">{text}</pre>
            ) : (
              <div className="p-doclist-loading">Loading…</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
