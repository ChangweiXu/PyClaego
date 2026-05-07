/**
 * MentionExtension.ts — Tiptap extension for @document link hints
 *
 * This is a visual-only extension that highlights @word patterns in text.
 * Actual document links are stored as regular links via the Tiptap Link extension.
 * The @popup in the editor inserts links, not mention nodes.
 */
import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

export interface MentionOptions {
  /** Called when a highlighted mention is clicked */
  onClick?: (mentionText: string) => void
}

export const MentionExtension = Extension.create<MentionOptions>({
  name: 'mentionHighlight',

  addOptions() {
    return {
      onClick: undefined,
    }
  },

  addProseMirrorPlugins() {
    return [mentionHighlightPlugin(this.options.onClick)]
  },
})

// Pattern to highlight @mentions in plain text
const MENTION_REGEX = /@([\w][\w-]*(?:\/[\w-]+)*(?:#[\w-]+)?)/g

function mentionHighlightPlugin(onClick?: (mentionText: string) => void): Plugin {
  const key = new PluginKey('mentionHighlight')

  return new Plugin({
    key,
    state: {
      init: () => DecorationSet.empty,
      apply(tr, _oldState) {
        if (!tr.docChanged) return _oldState

        const decorations: Decoration[] = []
        tr.doc.descendants((node, pos) => {
          if (!node.isText) return

          const text = node.text ?? ''
          let match
          MENTION_REGEX.lastIndex = 0
          while ((match = MENTION_REGEX.exec(text)) !== null) {
            const from = pos + match.index
            const to = from + match[0].length
            decorations.push(
              Decoration.inline(from, to, {
                class: 'tiptap-mention-highlight',
              }),
            )
          }
        })

        return DecorationSet.create(tr.doc, decorations)
      },
    },
    props: {
      decorations(state) {
        return key.getState(state)
      },
      handleClick(view, pos, _event) {
        if (!onClick) return false
        const resolved = view.state.doc.resolve(pos)
        const node = resolved.parent
        if (!node.isText) return false

        const text = node.text ?? ''
        const offset = pos - resolved.start()

        MENTION_REGEX.lastIndex = 0
        let match
        while ((match = MENTION_REGEX.exec(text)) !== null) {
          const from = match.index
          const to = from + match[0].length
          if (offset >= from && offset < to) {
            onClick(match[1])
            return true
          }
        }
        return false
      },
    },
  })
}
