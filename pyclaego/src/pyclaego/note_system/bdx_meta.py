"""BDX metadata helpers — read/write <bdx:meta> inside .bdx documents.

Replaces the old ``frontmatter.py`` YAML approach.
Meta is stored as XML elements inside <bdx:meta>, not YAML frontmatter.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from .bdx_parser import BDX_NS, BdxMeta

_NS = f"{{{BDX_NS}}}"
_NS_MAP = {"bdx": BDX_NS}

# Regex to replace/inject the <bdx:meta>...</bdx:meta> block
_META_RE = re.compile(
    r"(<bdx:meta\b[^>]*>).*?(</bdx:meta>)",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# meta_element_xml  — render BdxMeta as indented XML fragment
# ---------------------------------------------------------------------------

def meta_element_xml(meta: BdxMeta) -> str:
    """Return the <bdx:meta>…</bdx:meta> block as a 2-space-indented string."""
    lines = ["  <bdx:meta>"]
    if meta.doc_id:
        lines.append(f"    <bdx:doc_id>{meta.doc_id}</bdx:doc_id>")
    if meta.rel_path:
        lines.append(f"    <bdx:rel_path>{_escape(meta.rel_path)}</bdx:rel_path>")
    if meta.title:
        lines.append(f"    <bdx:title>{_escape(meta.title)}</bdx:title>")
    if meta.created_at is not None:
        lines.append(f"    <bdx:created_at>{meta.created_at}</bdx:created_at>")
    if meta.modified_at is not None:
        lines.append(f"    <bdx:modified_at>{meta.modified_at}</bdx:modified_at>")
    lines.append("  </bdx:meta>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# strip_meta  — read <bdx:meta> from raw XML, return (BdxMeta, body_xml)
# ---------------------------------------------------------------------------

def strip_meta(xml_text: str) -> tuple[BdxMeta, str]:
    """Parse the <bdx:meta> block from a raw .bdx file.

    Returns (BdxMeta, original_xml_text).  The original text is returned
    unchanged; callers use it as the body for writes.  On any parse error
    returns (empty BdxMeta, xml_text).
    """
    if not xml_text or not xml_text.strip():
        return BdxMeta(), xml_text
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return BdxMeta(), xml_text

    meta_el = root.find("bdx:meta", _NS_MAP)
    if meta_el is None:
        return BdxMeta(), xml_text

    def _t(tag: str) -> str | None:
        el = meta_el.find(f"bdx:{tag}", _NS_MAP)
        return (el.text or "").strip() or None if el is not None else None

    def _i(tag: str) -> int | None:
        v = _t(tag)
        try:
            return int(v) if v else None
        except (ValueError, TypeError):
            return None

    meta = BdxMeta(
        doc_id=_t("doc_id"),
        rel_path=_t("rel_path"),
        title=_t("title"),
        created_at=_i("created_at"),
        modified_at=_i("modified_at"),
    )
    return meta, xml_text


# ---------------------------------------------------------------------------
# inject_meta  — replace or insert <bdx:meta> into XML text
# ---------------------------------------------------------------------------

def inject_meta(xml_text: str, meta: BdxMeta) -> str:
    """Return the .bdx XML with the <bdx:meta> block updated to reflect *meta*.

    If no <bdx:meta> block exists yet (e.g. an empty doc), it is inserted.
    """
    new_meta_xml = meta_element_xml(meta)

    if _META_RE.search(xml_text):
        # Replace existing meta block
        return _META_RE.sub(new_meta_xml, xml_text, count=1)

    # No meta yet — insert after the opening <bdx:doc ...> tag
    doc_open_re = re.compile(r"(<bdx:doc\b[^>]*>)", re.DOTALL)
    m = doc_open_re.search(xml_text)
    if m:
        insert_pos = m.end()
        return xml_text[:insert_pos] + "\n" + new_meta_xml + xml_text[insert_pos:]

    # Fallback: prepend a whole doc wrapper
    from .bdx_serializer import empty_doc
    return empty_doc(meta)


# ---------------------------------------------------------------------------
# extract_title  — first heading text or None
# ---------------------------------------------------------------------------

def extract_title(xml_text: str) -> str | None:
    """Return the text of the first heading block, or None."""
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return None
    body = root.find("bdx:body", _NS_MAP)
    if body is None:
        return None
    for block in body:
        if block.get("type") == "heading":
            parts = []
            for child in block:
                if child.tag == f"{_NS}content" and child.text:
                    parts.append(child.text)
                elif child.tag == f"{_NS}link":
                    d = child.get("display", "")
                    if d:
                        parts.append(d)
            text = "".join(parts).strip()
            if text:
                return text
    return None


# ---------------------------------------------------------------------------
# meta_to_dict  — convert BdxMeta to a plain dict for serialization
# ---------------------------------------------------------------------------

def meta_to_dict(meta: BdxMeta) -> dict[str, Any]:
    return {
        "doc_id": meta.doc_id,
        "rel_path": meta.rel_path,
        "title": meta.title,
        "created_at": meta.created_at,
        "modified_at": meta.modified_at,
    }


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "BdxMeta",
    "extract_title",
    "inject_meta",
    "meta_element_xml",
    "meta_to_dict",
    "strip_meta",
]
