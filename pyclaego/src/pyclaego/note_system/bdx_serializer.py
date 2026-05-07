"""BDX XML serializer for the notes widget.

Responsibilities
----------------
1. ``serialize(meta, blocks)``  — canonical XML from a BdxMeta + block list.
2. ``fragment_json_to_xml(fragment)``  — convert LLM-emitted bdx-fragment JSON
   to a canonical XML string ready for insertion.
3. ``assign_block_ids(xml_text)``  — ensure every <bdx:block> has a stable id attr.
4. ``empty_doc(meta)`` — produce a minimal valid .bdx document.

Canonical form rules (for stable diffs)
-----------------------------------------
- Attributes in fixed order: type, level/lang/src/alt, id
- All text content wrapped in CDATA (``<![CDATA[...]]>``)
- Two-space indentation
- Trailing newline

bdx-fragment JSON schema (emitted by LLM tools)
-------------------------------------------------
A fragment is either a single block object or an array of block objects.

Block object::

    {
      "type": "paragraph",          // required
      "id": "b_xxxx",               // optional; assigned by serializer if absent
      "level": 1,                   // heading only
      "lang": "python",             // code only
      "content": [                  // array of inline items
        {"kind": "text", "text": "Hello "},
        {"kind": "link", "target": "other.bdx", "anchor": "b_ab12", "display": "link"},
        {"kind": "tag", "name": "research"},
        {"kind": "text", "text": " world."}
      ]
    }
"""

from __future__ import annotations

import random
import re
import string
from typing import Any

from .bdx_parser import BDX_NS, BdxMeta

_BID_CHARS = string.ascii_lowercase + string.digits
_BID_RE = re.compile(r"^b_[a-z0-9]{4,}$")
_EXISTING_ID_RE = re.compile(r'(<bdx:block\b[^>]*?)\bid="b_[a-z0-9]+"', re.DOTALL)
_BLOCK_OPEN_RE = re.compile(r"(<bdx:block\b)([^>]*?)(/>|>)", re.DOTALL)

_XMLNS = f'xmlns:bdx="{BDX_NS}"'


def _new_id() -> str:
    return "b_" + "".join(random.choices(_BID_CHARS, k=4))


def _cdata(text: str) -> str:
    """Wrap text in CDATA; handle the (rare) case of ]]> inside."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


# ---------------------------------------------------------------------------
# Empty doc
# ---------------------------------------------------------------------------

def empty_doc(meta: BdxMeta) -> str:
    """Return a minimal valid .bdx document with no body blocks."""
    from .bdx_meta import meta_element_xml
    meta_xml = meta_element_xml(meta)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<bdx:doc {_XMLNS}>\n'
        f'{meta_xml}\n'
        '  <bdx:body/>\n'
        '</bdx:doc>\n'
    )


# ---------------------------------------------------------------------------
# assign_block_ids  — ensures every <bdx:block> has an id
# ---------------------------------------------------------------------------

def assign_block_ids(xml_text: str) -> tuple[str, bool]:
    """Add missing id attributes to every <bdx:block> element.

    Returns ``(new_xml, changed)`` where *changed* is True if any id was
    added (caller should persist the updated file).
    """
    changed = False

    def _add_id(m: re.Match) -> str:
        nonlocal changed
        tag_open = m.group(1)  # e.g. '<bdx:block'
        attrs = m.group(2)     # e.g. ' type="paragraph"'
        close = m.group(3)     # '>' or '/>'
        if 'id="' in attrs:
            return m.group(0)
        changed = True
        new_id = _new_id()
        return f'{tag_open}{attrs} id="{new_id}"{close}'

    result = _BLOCK_OPEN_RE.sub(_add_id, xml_text)
    return result, changed


# ---------------------------------------------------------------------------
# serialize  — canonical XML from meta + raw blocks XML
# ---------------------------------------------------------------------------

def serialize(meta: BdxMeta, body_xml: str) -> str:
    """Wrap meta + pre-built body XML into a full .bdx document.

    ``body_xml`` should be everything that goes inside <bdx:body>...</bdx:body>,
    i.e. a sequence of <bdx:block> elements (no wrapper tag).
    """
    from .bdx_meta import meta_element_xml
    meta_xml = meta_element_xml(meta)
    indented_body = _indent_body(body_xml)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<bdx:doc {_XMLNS}>\n'
        f'{meta_xml}\n'
        '  <bdx:body>\n'
        f'{indented_body}'
        '  </bdx:body>\n'
        '</bdx:doc>\n'
    )


def _indent_body(body_xml: str) -> str:
    lines = body_xml.splitlines(keepends=True)
    return "".join("    " + line for line in lines if line.strip())


# ---------------------------------------------------------------------------
# fragment_json_to_xml  — LLM fragment JSON → XML blocks string
# ---------------------------------------------------------------------------

def fragment_json_to_xml(fragment: Any) -> str:
    """Convert a bdx-fragment JSON object (dict or list of dicts) to XML block elements.

    The returned string is a sequence of <bdx:block> elements suitable for
    insertion into a <bdx:body> or for use with ``serialize()``.

    Raises ``ValueError`` on structural errors (missing 'type', invalid inline kind).
    """
    if isinstance(fragment, dict):
        blocks = [fragment]
    elif isinstance(fragment, list):
        blocks = fragment
    else:
        raise ValueError(f"fragment must be a dict or list, got {type(fragment).__name__}")

    parts: list[str] = []
    for block in blocks:
        parts.append(_block_to_xml(block))
    return "\n".join(parts) + "\n"


def _block_to_xml(block: dict) -> str:
    kind = block.get("type")
    if not kind:
        raise ValueError("block missing required 'type' field")

    block_id = block.get("id") or _new_id()
    if not _BID_RE.match(block_id):
        block_id = _new_id()

    # Build opening tag attrs
    attrs = f'type="{_attr(kind)}"'
    if kind == "heading":
        lvl = block.get("level", 1)
        try:
            attrs += f' level="{int(lvl)}"'
        except (ValueError, TypeError):
            attrs += ' level="1"'
    elif kind == "code":
        lang = block.get("lang", "")
        attrs += f' lang="{_attr(str(lang))}"'
    elif kind == "image":
        src = block.get("src", "")
        alt = block.get("alt", "")
        attrs += f' src="{_attr(src)}" alt="{_attr(alt)}"'

    attrs += f' id="{block_id}"'

    content_items = block.get("content", [])
    if not content_items:
        return f'<bdx:block {attrs}/>'

    inner_parts: list[str] = []
    for item in content_items:
        item_kind = item.get("kind", "text")
        if item_kind == "text":
            text = item.get("text", "")
            if text:
                inner_parts.append(f"  <bdx:content>{_cdata(text)}</bdx:content>")
        elif item_kind == "link":
            target = item.get("target", "").strip()
            if not target:
                continue
            if not target.endswith(".bdx"):
                target += ".bdx"
            anchor = item.get("anchor", "")
            display = item.get("display", "")
            inner_parts.append(
                f'  <bdx:link target="{_attr(target)}" anchor="{_attr(anchor)}" display="{_attr(display)}"/>'
            )
        elif item_kind == "tag":
            name = item.get("name", "").strip().lower()
            if name:
                inner_parts.append(f'  <bdx:tag name="{_attr(name)}"/>')
        elif item_kind == "highlight":
            text = item.get("text", "")
            if text:
                color = item.get("color", "")
                if color:
                    inner_parts.append(
                        f'  <bdx:highlight color="{_attr(color)}">{_cdata(text)}</bdx:highlight>'
                    )
                else:
                    inner_parts.append(f"  <bdx:highlight>{_cdata(text)}</bdx:highlight>")
        elif item_kind in ("bold", "italic", "underline", "strike", "code",
                           "subscript", "superscript"):
            text = item.get("text", "")
            if text:
                inner_parts.append(
                    f"  <bdx:{item_kind}>{_cdata(text)}</bdx:{item_kind}>"
                )
        # Unknown kinds silently skipped

    if not inner_parts:
        return f'<bdx:block {attrs}/>'

    inner = "\n".join(inner_parts)
    return f"<bdx:block {attrs}>\n{inner}\n</bdx:block>"


def _attr(value: str) -> str:
    """XML-escape a value for use in an attribute."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "assign_block_ids",
    "empty_doc",
    "fragment_json_to_xml",
    "serialize",
]
