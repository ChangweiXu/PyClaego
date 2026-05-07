"""Tiptap JSON ↔ BDX XML converter.

Replaces the frontend bdxTransformer.ts — all BDX format knowledge lives here.

Functions:
    tiptap_json_to_xml(json, existing_xml=None) → str
        Convert Tiptap doc JSON to full .bdx XML string.
        If existing_xml is provided, preserves its <bdx:meta>.

    xml_to_tiptap_json(xml, from_path='') → dict
        Parse .bdx XML into Tiptap doc JSON.

Block type mapping:
    Tiptap paragraph      ↔  bdx paragraph
    Tiptap heading        ↔  bdx heading   (level 1-3)
    Tiptap codeBlock      ↔  bdx code      (language attr)
    Tiptap blockquote     ↔  bdx quote
    Tiptap bulletList     ↔  bdx list style="unordered"
    Tiptap orderedList    ↔  bdx list style="ordered"
    Tiptap taskList       ↔  bdx taskList  (taskItem with checked attr)
    Tiptap table          ↔  bdx table     (tableRow/tableHeader/tableCell)
    Tiptap image          ↔  bdx image     (src + alt)
    Tiptap horizontalRule ↔  bdx divider
    Tiptap paragraph      ↔  bdx callout   (ⓘ prefix)

Block-level text alignment (align attr on paragraph/heading):
    Tiptap textAlign mark → bdx:block align="left|center|right|justify"

Inline mark → BDX element mapping:
    bold       → <bdx:bold>
    italic     → <bdx:italic>
    underline  → <bdx:underline>
    strike     → <bdx:strike>
    code       → <bdx:code>
    highlight  → <bdx:highlight color="...">
    color      → <bdx:color value="...">
    subscript  → <bdx:subscript>
    superscript→ <bdx:superscript>
    link       → <bdx:link target="..." anchor="..." display="..."/>
    tag        → <bdx:tag name="..."/>

Marks can nest — e.g. bold+italic → <bdx:bold><bdx:italic>text</bdx:italic></bdx:bold>.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from .bdx_parser import BDX_NS, _local

_log = logging.getLogger(__name__)

_NS = f"{{{BDX_NS}}}"

# Marks that map to BDX wrapper elements (wrapping <bdx:content> inside)
_WRAPPER_MARKS: dict[str, str] = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strike": "strike",
    "code": "code",
    "superscript": "superscript",
    "subscript": "subscript",
}

# Reverse map: BDX element local name → Tiptap mark type
_WRAPPER_MARK_REVERSE: dict[str, str] = {
    v: k for k, v in _WRAPPER_MARKS.items()
}

# Special marks with attrs — these produce their own BDX output (not wrapper marks)
_SPECIAL_MARKS = {"link", "tag", "highlight", "textStyle"}

# textAlign values valid in both Tiptap and BDX
_VALID_ALIGNMENTS = {"left", "center", "right", "justify"}


def _cdata(text: str) -> str:
    """Wrap text in CDATA; handle ]]> inside text."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _esc_attr(s: str) -> str:
    """XML-escape a value for use in an attribute."""
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


# =============================================================================
# JSON → XML
# =============================================================================

def tiptap_json_to_xml(json: dict, existing_xml: str | None = None) -> str:
    """Convert Tiptap doc JSON to a full .bdx XML document.

    If existing_xml is provided, its <bdx:meta> is preserved.
    """
    meta_fragment = _extract_meta_raw(existing_xml) if existing_xml else ""
    doc_content = json.get("content", [])
    body_lines = [_node_to_xml(node) for node in doc_content]
    body_xml = "\n".join(body_lines)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<bdx:doc xmlns:bdx="{BDX_NS}">',
    ]
    if meta_fragment:
        parts.append(f"  {meta_fragment}")
    parts.append("  <bdx:body>")
    if body_xml:
        parts.append(body_xml)
    parts.append("  </bdx:body>")
    parts.append("</bdx:doc>")
    return "\n".join(parts) + "\n"


def _extract_meta_raw(xml: str) -> str:
    """Extract <bdx:meta>...</bdx:meta> as a raw string."""
    m = re.search(r"<bdx:meta[\s\S]*?</bdx:meta>", xml)
    return m.group(0) if m else ""


def _node_to_xml(node: dict) -> str:
    """Serialize a single Tiptap node to bdx XML block(s)."""
    attrs = node.get("attrs", {}) or {}
    block_id = attrs.get("id")
    id_attr = f' id="{_esc_attr(block_id)}"' if block_id else ""

    # textAlign — extracted from attrs, serialized as block-level align attribute
    text_align = attrs.get("textAlign")
    align_attr = f' align="{_esc_attr(text_align)}"' if text_align in _VALID_ALIGNMENTS else ""

    node_type = node.get("type", "paragraph")

    if node_type == "heading":
        level = attrs.get("level", 1)
        inner = _inline_to_xml(node.get("content"))
        return f'    <bdx:block type="heading" level="{int(level)}"{align_attr}{id_attr}>{inner}</bdx:block>'

    elif node_type == "codeBlock":
        lang = attrs.get("language", "")
        text = "".join(
            (n.get("text") or "") for n in (node.get("content") or [])
        )
        return (
            f'    <bdx:block type="code" lang="{_esc_attr(str(lang))}"{id_attr}>'
            f"<bdx:content>{_cdata(text)}</bdx:content>"
            f"</bdx:block>"
        )

    elif node_type == "blockquote":
        block_content = node.get("content") or []
        paragraph = next((n for n in block_content if n.get("type") == "paragraph"), None)
        inner = _inline_to_xml(paragraph.get("content")) if paragraph else ""
        return f'    <bdx:block type="quote"{id_attr}>{inner}</bdx:block>'

    elif node_type == "bulletList":
        items = _list_items_to_xml(node.get("content") or [])
        return f'    <bdx:block type="list" style="unordered"{id_attr}>{items}</bdx:block>'

    elif node_type == "orderedList":
        items = _list_items_to_xml(node.get("content") or [])
        return f'    <bdx:block type="list" style="ordered"{id_attr}>{items}</bdx:block>'

    elif node_type == "taskList":
        items = _task_items_to_xml(node.get("content") or [])
        return f'    <bdx:block type="taskList"{id_attr}>{items}</bdx:block>'

    elif node_type == "table":
        rows_xml = _table_rows_to_xml(node.get("content") or [])
        return f'    <bdx:block type="table"{id_attr}>{rows_xml}</bdx:block>'

    elif node_type == "image":
        src = attrs.get("src", "")
        alt = attrs.get("alt") or attrs.get("title") or ""
        return f'    <bdx:block type="image" src="{_esc_attr(src)}" alt="{_esc_attr(alt)}"{id_attr}/>'

    elif node_type == "horizontalRule":
        return f'    <bdx:block type="divider"{id_attr}/>'

    else:
        # paragraph (and unknown types → paragraph)
        inner = _inline_to_xml(node.get("content"))
        return f'    <bdx:block type="paragraph"{align_attr}{id_attr}>{inner}</bdx:block>'


def _list_items_to_xml(content: list) -> str:
    """Serialize list items to bdx:listItem XML fragments."""
    parts = []
    for li in (content or []):
        if li.get("type") != "listItem":
            continue
        li_attrs = li.get("attrs", {}) or {}
        li_id = li_attrs.get("id")
        li_id_attr = f' id="{_esc_attr(li_id)}"' if li_id else ""
        li_content = li.get("content") or []
        paragraph = next((n for n in li_content if n.get("type") == "paragraph"), None)
        inner = _inline_to_xml(paragraph.get("content")) if paragraph else ""
        parts.append(f"<bdx:listItem{li_id_attr}>{inner}</bdx:listItem>")
    return "".join(parts)


def _task_items_to_xml(content: list) -> str:
    """Serialize task list items to bdx:taskItem XML fragments with checked attr."""
    parts = []
    for ti in (content or []):
        if ti.get("type") != "taskItem":
            continue
        ti_attrs = ti.get("attrs", {}) or {}
        ti_id = ti_attrs.get("id")
        ti_id_attr = f' id="{_esc_attr(ti_id)}"' if ti_id else ""
        checked = ti_attrs.get("checked", False)
        checked_attr = ' checked="true"' if checked else ' checked="false"'
        ti_content = ti.get("content") or []
        paragraph = next((n for n in ti_content if n.get("type") == "paragraph"), None)
        inner = _inline_to_xml(paragraph.get("content")) if paragraph else ""
        parts.append(f"<bdx:taskItem{checked_attr}{ti_id_attr}>{inner}</bdx:taskItem>")
    return "".join(parts)


def _table_rows_to_xml(rows: list) -> str:
    """Serialize table rows to bdx:tableRow XML fragments with tableHeader/tableCell."""
    parts = []
    for row in (rows or []):
        if row.get("type") != "tableRow":
            continue
        cells: list[str] = []
        for cell in (row.get("content") or []):
            cell_type = cell.get("type", "tableCell")
            if cell_type not in ("tableCell", "tableHeader"):
                continue
            # Extract inline content from the paragraph inside the cell
            cell_content = cell.get("content") or []
            paragraph = next((n for n in cell_content if n.get("type") == "paragraph"), None)
            inner = _inline_to_xml(paragraph.get("content")) if paragraph else ""
            tag = f"bdx:{cell_type}"
            cells.append(f"<{tag}>{inner}</{tag}>")
        parts.append(f"<bdx:tableRow>{''.join(cells)}</bdx:tableRow>")
    return "".join(parts)


# Map of Tiptap mark type → bdx element name
_MARK_TO_ELEMENT: dict[str, str] = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strike": "strike",
    "code": "code",
    "superscript": "superscript",
    "subscript": "subscript",
}


def _inline_to_xml(content: list | None) -> str:
    """Serialize inline Tiptap content nodes to bdx XML fragments."""
    if not content:
        return ""
    parts: list[str] = []
    for node in content:
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text") or ""
            if not text:
                continue
            marks = node.get("marks") or []

            # Classify marks: wrapper (bold, italic, etc.), wrapper-special
            # (highlight, color — produce BDX wrapper elements), and
            # self-closing (link, tag — emit directly and skip wrapper marks).
            wrapper_marks = []
            wrapper_special_marks = []
            has_self_closing = False

            for m in marks:
                m_type = m.get("type", "")
                m_attrs = m.get("attrs", {}) or {}
                if m_type in ("link", "tag"):
                    # Self-closing: emit directly
                    if m_type == "link":
                        href = m_attrs.get("href", "")
                        hash_idx = href.rfind("#") if "#" in href else -1
                        target = href[:hash_idx] if hash_idx > 0 else href
                        anchor = href[hash_idx + 1:] if hash_idx > 0 else ""
                        parts.append(
                            f'<bdx:link target="{_esc_attr(target)}" '
                            f'anchor="{_esc_attr(anchor)}" display="{_esc_attr(text)}"/>'
                        )
                    elif m_type == "tag":
                        tag_name = m_attrs.get("name", "")
                        parts.append(f'<bdx:tag name="{_esc_attr(tag_name)}"/>')
                    has_self_closing = True
                elif m_type == "highlight":
                    color = m_attrs.get("color", "")
                    wrapper_special_marks.append(("highlight", color))
                elif m_type == "textStyle":
                    color = m_attrs.get("color", "")
                    if color:
                        wrapper_special_marks.append(("color", color))
                    # textStyle without color is no-op
                elif m_type in _MARK_TO_ELEMENT:
                    wrapper_marks.append(m)

            # Self-closing marks (link/tag) produce their own complete output;
            # wrapper marks cannot be combined with them.
            if has_self_closing:
                continue

            # Build from inner to outer:
            # innermost: <bdx:content>text</bdx:content>
            # then: wrapper-special marks (color, highlight)
            # outermost: wrapper marks (bold, italic, etc.)
            inner = f"<bdx:content>{_cdata(text)}</bdx:content>"
            for spec_type, spec_attr in wrapper_special_marks:
                if spec_type == "highlight":
                    if spec_attr:
                        inner = f'<bdx:highlight color="{_esc_attr(spec_attr)}">{inner}</bdx:highlight>'
                    else:
                        inner = f"<bdx:highlight>{inner}</bdx:highlight>"
                elif spec_type == "color":
                    inner = f'<bdx:color value="{_esc_attr(spec_attr)}">{inner}</bdx:color>'
            for m in reversed(wrapper_marks):
                el_name = _MARK_TO_ELEMENT[m.get("type", "")]
                inner = f"<bdx:{el_name}>{inner}</bdx:{el_name}>"
            parts.append(inner)

        elif node_type == "mention":
            # Mentions are serialized as bdx:link elements
            n_attrs = node.get("attrs", {}) or {}
            rel_path = n_attrs.get("relPath", "")
            block_id = n_attrs.get("blockId", "")
            display_text = n_attrs.get("displayText", rel_path)
            parts.append(
                f'<bdx:link target="{_esc_attr(rel_path)}" '
                f'anchor="{_esc_attr(block_id)}" display="{_esc_attr(f"@{display_text}")}"/>'
            )

    return "".join(parts)


# =============================================================================
# XML → JSON
# =============================================================================

def xml_to_tiptap_json(xml: str, from_path: str = "") -> dict:
    """Parse .bdx XML into Tiptap doc JSON."""
    if not xml or not xml.strip():
        return {"type": "doc", "content": [{"type": "paragraph"}]}

    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        _log.warning("tiptap_json_converter: XML parse error")
        return {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "⚠ Document parse error"}]}],
        }

    ns = {"bdx": BDX_NS}
    body_el = root.find("bdx:body", ns)
    if body_el is None:
        return {"type": "doc", "content": [{"type": "paragraph"}]}

    content: list[dict] = []
    for child in body_el:
        if _local(child.tag) != "block":
            continue
        block = _parse_block_to_json(child, ns, from_path)
        if block is not None:
            content.append(block)

    return {
        "type": "doc",
        "content": content if content else [{"type": "paragraph"}],
    }


def _parse_block_to_json(el: ET.Element, ns: dict, from_path: str) -> dict | None:
    """Parse a single <bdx:block> element to a Tiptap node dict."""
    block_type = el.get("type", "paragraph")
    block_id = el.get("id")
    text_align = el.get("align")

    if block_type == "heading":
        try:
            level = int(el.get("level", "1"))
        except (ValueError, TypeError):
            level = 1
        level = min(3, max(1, level))
        inline = _parse_inline_children(el, ns, from_path)
        attrs: dict = {"level": level}
        if block_id:
            attrs["id"] = block_id
        if text_align in _VALID_ALIGNMENTS:
            attrs["textAlign"] = text_align
        return {"type": "heading", "attrs": attrs, "content": inline}

    elif block_type == "code":
        lang = el.get("lang", "")
        text = _block_text(el, ns)
        attrs = {"language": lang}
        if block_id:
            attrs["id"] = block_id
        return {
            "type": "codeBlock",
            "attrs": attrs,
            "content": [{"type": "text", "text": text}],
        }

    elif block_type == "quote":
        inline = _parse_inline_children(el, ns, from_path)
        result: dict = {
            "type": "blockquote",
            "content": [{"type": "paragraph", "content": inline}],
        }
        if block_id:
            result["attrs"] = {"id": block_id}
        return result

    elif block_type == "list":
        style = el.get("style", "unordered")
        list_items: list[dict] = []
        for child in el:
            if _local(child.tag) != "listItem":
                continue
            li_id = child.get("id")
            inline = _parse_inline_children(child, ns, from_path)
            li_node: dict = {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": inline}],
            }
            if li_id:
                li_node["attrs"] = {"id": li_id}
            list_items.append(li_node)
        list_type = "orderedList" if style == "ordered" else "bulletList"
        result = {"type": list_type, "content": list_items}
        if block_id:
            result["attrs"] = {"id": block_id}
        return result

    elif block_type == "taskList":
        task_items: list[dict] = []
        for child in el:
            if _local(child.tag) != "taskItem":
                continue
            ti_id = child.get("id")
            checked_str = child.get("checked", "false")
            checked = checked_str.lower() == "true"
            inline = _parse_inline_children(child, ns, from_path)
            ti_node: dict = {
                "type": "taskItem",
                "attrs": {"checked": checked},
                "content": [{"type": "paragraph", "content": inline}],
            }
            if ti_id:
                ti_node["attrs"]["id"] = ti_id
            task_items.append(ti_node)
        result = {"type": "taskList", "content": task_items}
        if block_id:
            result["attrs"] = {"id": block_id}
        return result

    elif block_type == "table":
        table_rows: list[dict] = []
        for child in el:
            if _local(child.tag) != "tableRow":
                continue
            row_cells: list[dict] = []
            for cell in child:
                cell_local = _local(cell.tag)
                if cell_local not in ("tableCell", "tableHeader"):
                    continue
                cell_inline = _parse_inline_children(cell, ns, from_path)
                cell_node: dict = {
                    "type": cell_local,
                    "content": [{"type": "paragraph", "content": cell_inline}]
                    if cell_inline else [{"type": "paragraph"}],
                }
                row_cells.append(cell_node)
            row_node: dict = {"type": "tableRow", "content": row_cells}
            table_rows.append(row_node)
        result = {"type": "table", "content": table_rows}
        if block_id:
            result["attrs"] = {"id": block_id}
        return result

    elif block_type == "image":
        src = el.get("src", "")
        alt = el.get("alt", "")
        attrs = {"src": src, "alt": alt, "title": alt}
        if block_id:
            attrs["id"] = block_id
        return {"type": "image", "attrs": attrs}

    elif block_type == "divider":
        result = {"type": "horizontalRule"}
        if block_id:
            result["attrs"] = {"id": block_id}
        return result

    elif block_type == "callout":
        text = _block_text(el, ns)
        attrs: dict = {}
        if block_id:
            attrs["id"] = block_id
        if text_align in _VALID_ALIGNMENTS:
            attrs["textAlign"] = text_align
        result = {
            "type": "paragraph",
            "content": [{"type": "text", "text": f"ⓘ {text}"}],
        }
        if attrs:
            result["attrs"] = attrs
        return result

    else:
        # paragraph (and unknown types → paragraph)
        inline = _parse_inline_children(el, ns, from_path)
        attrs: dict = {}
        if block_id:
            attrs["id"] = block_id
        if text_align in _VALID_ALIGNMENTS:
            attrs["textAlign"] = text_align
        result = {"type": "paragraph", "content": inline}
        if attrs:
            result["attrs"] = attrs
        return result


def _block_text(el: ET.Element, ns: dict) -> str:
    """Extract plain text from all <bdx:content> children of an element."""
    parts = []
    for child in el:
        if _local(child.tag) == "content":
            parts.append(child.text or "")
    return "".join(parts)


def _parse_inline_children(
    el: ET.Element, ns: dict, from_path: str, _inherited_marks: list[dict] | None = None
) -> list[dict]:
    """Parse inline children of a BDX element into Tiptap text nodes with marks.

    Recursively enters wrapper mark elements (bold, italic, etc.) to build
    mark stacks for nested formatting.
    """
    nodes: list[dict] = []
    marks_stack = list(_inherited_marks or [])

    for child in el:
        local = _local(child.tag)

        if local == "content":
            text = child.text or ""
            if text:
                node: dict = {"type": "text", "text": text}
                if marks_stack:
                    node["marks"] = list(marks_stack)
                nodes.append(node)

        elif local == "link":
            target = child.get("target", "")
            display = child.get("display", target)
            anchor = child.get("anchor", "")
            href = f"{target}#{anchor}" if anchor else target
            nodes.append({
                "type": "text",
                "text": display,
                "marks": list(marks_stack) + [{"type": "link", "attrs": {"href": href}}],
            })

        elif local == "tag":
            tag_name = child.get("name", "")
            nodes.append({
                "type": "text",
                "text": f"#{tag_name}",
                "marks": list(marks_stack) + [{"type": "tag", "attrs": {"name": tag_name}}],
            })

        elif local == "mention":
            target = child.get("target", "")
            display = child.get("display", target)
            anchor = child.get("anchor", "")
            href = f"{target}#{anchor}" if anchor else target
            nodes.append({
                "type": "text",
                "text": f"@{display}",
                "marks": list(marks_stack) + [{"type": "link", "attrs": {"href": href}}],
            })

        elif local == "highlight":
            color = child.get("color", "")
            highlight_mark: dict = {"type": "highlight"}
            if color:
                highlight_mark["attrs"] = {"color": color}
            marks_stack.append(highlight_mark)
            nodes.extend(_parse_inline_children(child, ns, from_path, marks_stack))
            marks_stack.pop()

        elif local == "color":
            value = child.get("value", "")
            color_mark: dict = {"type": "textStyle"}
            if value:
                color_mark["attrs"] = {"color": value}
            marks_stack.append(color_mark)
            nodes.extend(_parse_inline_children(child, ns, from_path, marks_stack))
            marks_stack.pop()

        elif local in _WRAPPER_MARK_REVERSE:
            # Wrapper marks: bold, italic, underline, strike, code, sub, sup
            mark_type = _WRAPPER_MARK_REVERSE[local]
            marks_stack.append({"type": mark_type})
            nodes.extend(_parse_inline_children(child, ns, from_path, marks_stack))
            marks_stack.pop()

        elif child.text and child.text.strip():
            # Unknown element with direct text — treat as plain text (lenient)
            nodes.append({"type": "text", "text": child.text})

    # Fallback: if no nodes produced but element has direct text
    if not nodes:
        text = (el.text or "").strip()
        if text:
            node = {"type": "text", "text": text}
            if marks_stack:
                node["marks"] = list(marks_stack)
            nodes.append(node)

    return nodes
