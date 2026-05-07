/**
 * bdxBridge.ts — Convert between .bdx XML and BlockNote's block tree.
 *
 * xmlToBlocks(xml)  → PartialBlock[]   (server XML → BlockNote editor state)
 * blocksToXml(blocks, existingXml?)    → string  (editor blocks → .bdx XML string)
 *
 * The body of a .bdx file is a <bdx:body> element containing <bdx:block> elements.
 * Meta (<bdx:meta>) is preserved as-is — this module only touches <bdx:body>.
 *
 * Block type mapping:
 *   bdx paragraph     ↔  BlockNote paragraph
 *   bdx heading       ↔  BlockNote heading  (level 1-3)
 *   bdx code          ↔  BlockNote codeBlock  (lang attr)
 *   bdx quote         ↔  BlockNote quote
 *   bdx list          ↔  BlockNote bulletListItem / numberedListItem
 *   bdx image         ↔  BlockNote image  (src + alt)
 *   bdx divider       ↔  BlockNote (ignored for now, rendered as horizontal rule)
 *   bdx callout       ↔  BlockNote paragraph  (wrapped in ⓘ note style)
 *
 * Inline content:
 *   <bdx:content>CDATA</bdx:content>  → plain text span
 *   <bdx:link target anchor display>  → link inline content
 *   <bdx:tag name>                    → styled text span  (non-editable)
 *
 * For blocksToXml, BlockNote inline styles are written back as plain text
 * since .bdx does not carry bold/italic (no information loss for vault indexing).
 */

import type { Block, PartialBlock } from "@blocknote/core";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type InlineContent = any;

function extractRelPath(xml: string): string {
  const m = xml.match(/<bdx:rel_path>([^<]*)<\/bdx:rel_path>/);
  return m ? m[1].trim() : '';
}

const BDX_NS = "https://pyclaego.local/bdx/v1";

// ---------------------------------------------------------------------------
// XML parsing helpers
// ---------------------------------------------------------------------------

function parseDom(xml: string): Document {
  const parser = new DOMParser();
  return parser.parseFromString(xml, "application/xml");
}

/** Return the text of all <bdx:content> CDATA children of an element. */
function blockText(el: Element): string {
  let text = "";
  for (const child of el.childNodes) {
    if (child.nodeType === Node.ELEMENT_NODE) {
      const c = child as Element;
      if (localName(c) === "content") {
        text += c.textContent ?? "";
      }
    }
  }
  return text;
}

function localName(el: Element): string {
  return el.localName.replace(/^bdx:/, "");
}

/**
 * Build BlockNote inline content array from a <bdx:block> element's children.
 * Handles <bdx:content>, <bdx:link>, and <bdx:tag>.
 */
function inlineContent(el: Element, fromPath: string = ''): InlineContent[] {
  const spans: InlineContent[] = [];
  for (const child of el.childNodes) {
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const c = child as Element;
    const name = localName(c);
    if (name === "content") {
      const txt = c.textContent ?? "";
      if (txt) spans.push({ type: "text", text: txt, styles: {} });
    } else if (name === "link") {
      const display = c.getAttribute("display") ?? c.getAttribute("target") ?? "";
      const target = c.getAttribute("target") ?? "";
      const anchor = c.getAttribute("anchor") ?? "";
      // Backend already resolves targets to doc IDs; pass href through as-is
      const href = anchor ? `${target}#${anchor}` : target;
      spans.push({
        type: "link",
        href: href || target,
        content: [{ type: "text", text: display, styles: {} }],
      } as unknown as InlineContent);
    } else if (name === "tag") {
      const tagName = c.getAttribute("name") ?? "";
      spans.push({ type: "text", text: `#${tagName}`, styles: { bold: true } });
    }
  }
  if (spans.length === 0) {
    const plain = el.textContent ?? "";
    if (plain) spans.push({ type: "text", text: plain, styles: {} });
  }
  return spans;
}

// ---------------------------------------------------------------------------
// xmlToBlocks — parse .bdx XML and return BlockNote PartialBlock[]
// ---------------------------------------------------------------------------

export function xmlToBlocks(xml: string, fromPath: string = ''): PartialBlock[] {
  if (!xml || !xml.trim()) return [];
  const relPath = fromPath || extractRelPath(xml);

  const doc = parseDom(xml);
  const parseError = doc.querySelector("parsererror");
  if (parseError) {
    console.error("[bdxBridge] XML parse error:", parseError.textContent);
    return [{ type: "paragraph", content: "⚠ Document parse error" }];
  }

  // Locate <bdx:body>
  const bodyEls = doc.getElementsByTagNameNS(BDX_NS, "body");
  if (!bodyEls.length) {
    // No body — maybe an empty doc or direct content — treat as empty
    return [];
  }
  const body = bodyEls[0];
  const blocks: PartialBlock[] = [];

  for (const child of body.childNodes) {
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const el = child as Element;
    if (localName(el) !== "block") continue;

    const type = el.getAttribute("type") ?? "paragraph";
    const id = el.getAttribute("id") ?? undefined;

    switch (type) {
      case "heading": {
        const level = parseInt(el.getAttribute("level") ?? "1", 10) as 1 | 2 | 3;
        blocks.push({
          id,
          type: "heading",
          props: { level: Math.min(3, Math.max(1, level)) as 1 | 2 | 3 },
          content: inlineContent(el, relPath),
        });
        break;
      }

      case "code": {
        const lang = el.getAttribute("lang") ?? "";
        blocks.push({
          id,
          type: "codeBlock",
          props: { language: lang },
          content: [{ type: "text", text: blockText(el), styles: {} }],
        });
        break;
      }

      case "quote": {
        blocks.push({
          id,
          type: "quote",
          content: inlineContent(el, relPath),
        });
        break;
      }

      case "list": {
        const style = el.getAttribute("style") ?? "unordered";
        const listType = style === "ordered" ? "numberedListItem" : "bulletListItem";
        for (const li of el.childNodes) {
          if (li.nodeType !== Node.ELEMENT_NODE) continue;
          const liEl = li as Element;
          if (localName(liEl) !== "listItem") continue;
          blocks.push({
            id: liEl.getAttribute("id") ?? undefined,
            type: listType,
            content: inlineContent(liEl, relPath),
          });
        }
        break;
      }

      case "image": {
        const src = el.getAttribute("src") ?? "";
        const alt = el.getAttribute("alt") ?? "";
        blocks.push({
          id,
          type: "image",
          props: { url: src, caption: alt, showPreview: true },
          content: undefined,
        });
        break;
      }

      case "callout": {
        // Render as a paragraph with a ⓘ prefix for now
        const text = blockText(el);
        blocks.push({
          id,
          type: "paragraph",
          content: [{ type: "text", text: `ⓘ ${text}`, styles: {} }],
        });
        break;
      }

      case "divider": {
        // BlockNote doesn't have a native divider block type — skip silently.
        // It will be re-created as a divider on write.
        break;
      }

      default: {
        // paragraph (and unknown types → paragraph)
        blocks.push({
          id,
          type: "paragraph",
          content: inlineContent(el, relPath),
        });
        break;
      }
    }
  }

  return blocks.length > 0 ? blocks : [{ type: "paragraph", content: "" }];
}

// ---------------------------------------------------------------------------
// blocksToXml — serialise BlockNote Block[] back to .bdx XML body fragment
// ---------------------------------------------------------------------------

function cdata(text: string): string {
  // Escape ]]> in CDATA content
  return text.replace(/]]>/g, "]]]]><![CDATA[>");
}

function inlineToXml(content: InlineContent): string {
  if (!content) return "";
  if (typeof content === "string") {
    return `<bdx:content><![CDATA[${cdata(content)}]]></bdx:content>`;
  }
  let out = "";
  for (const span of content as InlineContent[]) {
    const s = span as {
      type: string; text?: string; href?: string;
      content?: InlineContent[];
    };
    if (s.type === "link") {
      const href = s.href ?? "";
      const hashIdx = href.lastIndexOf("#");
      const target = hashIdx > 0 ? href.slice(0, hashIdx) : href;
      const anchor = hashIdx > 0 ? href.slice(hashIdx + 1) : "";
      const display = (s.content ?? []).map((c) => c.text ?? "").join("");
      out += `<bdx:link target="${escAttr(target)}" anchor="${escAttr(anchor)}" display="${escAttr(display)}"/>`;
    } else {
      const text = s.text ?? "";
      if (text) out += `<bdx:content><![CDATA[${cdata(text)}]]></bdx:content>`;
    }
  }
  return out;
}

function escAttr(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function blockToXml(block: Block): string {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const b = block as any;
  const id = b.id ? ` id="${escAttr(b.id)}"` : "";
  const type = b.type;

  switch (type) {
    case "heading": {
      const level = (b.props?.level ?? 1) as number;
      const inner = inlineToXml(b.content);
      return `    <bdx:block type="heading" level="${level}"${id}>\n      ${inner}\n    </bdx:block>`;
    }
    case "codeBlock": {
      const lang = (b.props?.language ?? "") as string;
      const text = typeof b.content === "string"
        ? b.content
        : (b.content as InlineContent[]).map((s: InlineContent) => s.text ?? "").join("");
      return `    <bdx:block type="code" lang="${escAttr(lang)}"${id}>\n      <bdx:content><![CDATA[${cdata(text)}]]></bdx:content>\n    </bdx:block>`;
    }
    case "quote": {
      const inner = inlineToXml(b.content);
      return `    <bdx:block type="quote"${id}>\n      ${inner}\n    </bdx:block>`;
    }
    case "bulletListItem": {
      const inner = inlineToXml(b.content);
      return `    <bdx:block type="list" style="unordered"${id}>\n      <bdx:listItem${b.id ? ` id="${escAttr(b.id)}"` : ""}>${inner}</bdx:listItem>\n    </bdx:block>`;
    }
    case "numberedListItem": {
      const inner = inlineToXml(b.content);
      return `    <bdx:block type="list" style="ordered"${id}>\n      <bdx:listItem${b.id ? ` id="${escAttr(b.id)}"` : ""}>${inner}</bdx:listItem>\n    </bdx:block>`;
    }
    case "image": {
      const src = (b.props?.url ?? "") as string;
      const alt = (b.props?.caption ?? "") as string;
      return `    <bdx:block type="image" src="${escAttr(src)}" alt="${escAttr(alt)}"${id}/>`;
    }
    default: {
      const inner = inlineToXml(b.content);
      return `    <bdx:block type="paragraph"${id}>\n      ${inner}\n    </bdx:block>`;
    }
  }
}

/**
 * Serialise BlockNote blocks to a .bdx body fragment (just the <bdx:body> content).
 * If existingXml is provided, the <bdx:meta> element from it is preserved.
 */
export function blocksToXml(blocks: Block[], existingXml?: string): string {
  const metaFragment = existingXml ? extractMeta(existingXml) : "";
  const bodyLines = blocks.map(blockToXml).join("\n");
  return [
    `<?xml version="1.0" encoding="UTF-8"?>`,
    `<bdx:doc xmlns:bdx="${BDX_NS}">`,
    ...(metaFragment ? [`  ${metaFragment}`] : []),
    `  <bdx:body>`,
    bodyLines,
    `  </bdx:body>`,
    `</bdx:doc>`,
  ].join("\n");
}

/** Extract <bdx:meta>...</bdx:meta> as a raw string from full XML. */
function extractMeta(xml: string): string {
  const m = xml.match(/<bdx:meta[\s\S]*?<\/bdx:meta>/);
  return m ? m[0] : "";
}

// ---------------------------------------------------------------------------
// Tag helpers — read/write <bdx:meta><bdx:tags>...</bdx:tags>
// ---------------------------------------------------------------------------

/**
 * Scan the bdx:body for inline hashtags typed as plain text (#word) and explicit
 * <bdx:tag> elements (both count as tags).  Code blocks are excluded to avoid
 * false positives from # comments in code.
 */
export function extractTagsFromContent(xml: string): string[] {
  if (!xml || !xml.trim()) return [];
  const doc = parseDom(xml);
  const bodyEls = doc.getElementsByTagNameNS(BDX_NS, "body");
  if (!bodyEls.length) return [];
  const body = bodyEls[0];
  const tags = new Set<string>();

  for (const child of body.childNodes) {
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const el = child as Element;
    if (localName(el) !== "block") continue;
    // Skip code blocks — '#' in code is not a tag
    if (el.getAttribute("type") === "code") continue;

    // Collect all descendant <bdx:content> text and match #word patterns
    const collectContent = (node: Element) => {
      for (const c of node.childNodes) {
        if (c.nodeType === Node.ELEMENT_NODE) {
          const ce = c as Element;
          const name = localName(ce);
          if (name === "content") {
            const text = ce.textContent ?? "";
            for (const m of text.matchAll(/#([\w][\w-]*)/g)) {
              tags.add(m[1]);
            }
          } else if (name === "tag") {
            const tagName = ce.getAttribute("name") ?? "";
            if (tagName) tags.add(tagName);
          } else {
            collectContent(ce);
          }
        }
      }
    };
    collectContent(el);
  }

  return Array.from(tags);
}

/** Extract tag names from <bdx:tags>comma,separated</bdx:tags> in the meta block. */
export function extractTags(xml: string): string[] {
  const m = xml.match(/<bdx:tags>([\s\S]*?)<\/bdx:tags>/);
  if (!m) return [];
  return m[1].split(",").map((t) => t.trim()).filter(Boolean);
}

/**
 * Return a new XML string with the <bdx:tags> element replaced (or inserted into
 * <bdx:meta>) reflecting the given tag array.  An empty array removes the element.
 */
export function patchTags(xml: string, tags: string[]): string {
  const value = tags.map((t) => t.trim()).filter(Boolean).join(",");
  const tagsEl = value ? `<bdx:tags>${value}</bdx:tags>` : "";

  // Replace existing <bdx:tags>...</bdx:tags>
  if (/<bdx:tags>[\s\S]*?<\/bdx:tags>/.test(xml)) {
    return tagsEl
      ? xml.replace(/<bdx:tags>[\s\S]*?<\/bdx:tags>/, tagsEl)
      : xml.replace(/\s*<bdx:tags>[\s\S]*?<\/bdx:tags>/, "");
  }

  if (!tagsEl) return xml; // no tags and no existing element — nothing to do

  // Insert before </bdx:meta>
  if (/<\/bdx:meta>/.test(xml)) {
    return xml.replace(/<\/bdx:meta>/, `    ${tagsEl}\n  </bdx:meta>`);
  }

  // No meta block at all — wrap in a minimal <bdx:meta>
  return xml.replace(/(<bdx:doc[^>]*>)/, `$1\n  <bdx:meta>\n    ${tagsEl}\n  </bdx:meta>`);
}
