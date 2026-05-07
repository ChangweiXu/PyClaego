/**
 * TagExtension.ts — Tiptap Mark extension for #tags
 *
 * Renders tags as styled inline chips: #tagname
 * Stored in bdx XML as: <bdx:tag name="tagname"/>
 */
import { Mark, mergeAttributes } from '@tiptap/core'

export interface TagOptions {
  HTMLAttributes: Record<string, unknown>
}

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    tag: {
      /** Insert a tag at the current position */
      insertTag: (name: string) => ReturnType
    }
  }
}

export const TagExtension = Mark.create<TagOptions>({
  name: 'tag',

  addOptions() {
    return {
      HTMLAttributes: {},
    }
  },

  addAttributes() {
    return {
      name: {
        default: null,
        parseHTML: (element) => element.getAttribute('data-tag-name'),
        renderHTML: (attributes) => {
          if (!attributes.name) return {}
          return { 'data-tag-name': attributes.name }
        },
      },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-type="tag"]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes(HTMLAttributes, { 'data-type': 'tag', class: 'tiptap-tag' }), 0]
  },

  addCommands() {
    return {
      insertTag: (name: string) => ({ chain }) => {
        return chain()
          .insertContent({
            type: 'text',
            text: `#${name}`,
            marks: [{ type: 'tag', attrs: { name } }],
          })
          .run()
      },
    }
  },
})
