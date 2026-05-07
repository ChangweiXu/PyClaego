/**
 * JsonModal — centered pop-up dialog showing JSON (or text) content
 * in a read-only, monospace textarea with basic JSON syntax coloring
 * applied via the `.json-view` class (see styles.css).
 */
import { useState } from 'react';
import { HighlightedJson, toJsonString } from '../primitives/HighlightedJson';

interface JsonModalProps {
  open: boolean;
  title?: string;
  json: object | string;
  onClose: () => void;
}

export default function JsonModal({ open, title, json, onClose }: JsonModalProps) {
  const [copied, setCopied] = useState(false);
  if (!open) return null;
  const jsonString = toJsonString(json);

  const handleCopy = () => {
    navigator.clipboard.writeText(jsonString).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal json-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="json-modal-header">
          <h2 style={{ margin: 0, flex: 1 }}>{title ?? 'Details'}</h2>
          <button className="artifact-view-btn" onClick={handleCopy}>
            {copied ? 'copied!' : 'copy'}
          </button>
          <button className="artifact-view-btn" onClick={onClose}>close</button>
        </div>
        <HighlightedJson text={jsonString} />
      </div>
    </div>
  );
}
