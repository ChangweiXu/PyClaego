import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MarkdownSchema } from '../schema/types';

export function Markdown({ text }: MarkdownSchema) {
  return (
    <div className="p-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
