/**
 * HighlightedJson — shared primitive for rendering JSON with minimal syntax
 * highlighting (keys, strings, numbers, booleans/null).
 *
 * Used by JsonModal and HistoryModal.
 */

export function toJsonString(json: object | string): string {
  if (typeof json === 'string') {
    const trimmed = json.trim();
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try { return JSON.stringify(JSON.parse(trimmed), null, 2); } catch { /* keep */ }
    }
    return json;
  }
  try { return JSON.stringify(json, null, 2); } catch { return String(json); }
}

/**
 * Render JSON text with minimal syntax highlighting (keys, strings, numbers,
 * booleans/null). Falls back to plain text for non-JSON content.
 */
export function HighlightedJson({ text }: { text: string }) {
  // Very small tokenizer: good-enough for task metadata / artifact JSON.
  const tokens: { value: string; cls: string }[] = [];
  const re = /("(?:\\.|[^"\\])*"\s*:?)|('(?:\\.|[^'\\])*')|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}\[\],])|(\s+)|([^\s"{}\[\],]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m[1]) {
      const isKey = m[1].trimEnd().endsWith(':');
      tokens.push({ value: m[1], cls: isKey ? 'json-key' : 'json-string' });
    } else if (m[2]) tokens.push({ value: m[2], cls: 'json-string' });
    else if (m[3]) tokens.push({ value: m[3], cls: 'json-bool' });
    else if (m[4]) tokens.push({ value: m[4], cls: 'json-number' });
    else if (m[5]) tokens.push({ value: m[5], cls: 'json-punct' });
    else tokens.push({ value: m[0], cls: '' });
  }
  return (
    <pre className="json-view">
      {tokens.map((t, i) =>
        t.cls ? <span key={i} className={t.cls}>{t.value}</span> : t.value
      )}
    </pre>
  );
}
