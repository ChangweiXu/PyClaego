"""BDX XML parser for the notes widget.

.bdx file format — Phase 0 specification
=========================================

A .bdx file is a UTF-8 XML document using the ``bdx:`` namespace.

Namespace: ``https://pyclaego.local/bdx/v1``

Top-level structure::

    <?xml version="1.0" encoding="UTF-8"?>
    <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
      <bdx:meta>
        <bdx:doc_id>uuid-v4</bdx:doc_id>
        <bdx:rel_path>folder/note.bdx</bdx:rel_path>
        <bdx:title>Note Title</bdx:title>
        <bdx:created_at>1705318200000</bdx:created_at>
        <bdx:modified_at>1705404000000</bdx:modified_at>
      </bdx:meta>
      <bdx:body>
        <bdx:block type="heading" level="1" id="b_a3f9">
          <bdx:content><![CDATA[Introduction]]></bdx:content>
        </bdx:block>
        <bdx:block type="paragraph" id="b_d2e1">
          <bdx:content><![CDATA[Normal text with a ]]></bdx:content>
          <bdx:link target="other/note.bdx" anchor="b_x1y2" display="link text"/>
          <bdx:content><![CDATA[ and a ]]></bdx:content>
          <bdx:tag name="research"/>
          <bdx:content><![CDATA[ tag.]]></bdx:content>
        </bdx:block>
        <bdx:block type="code" lang="python" id="b_c8a1">
          <bdx:content><![CDATA[def hello():
    print("world")
]]></bdx:content>
        </bdx:block>
      </bdx:body>
    </bdx:doc>

Block types
-----------
paragraph, heading (+ level attr), code (+ lang attr), quote, list, listItem,
taskList, taskItem (+ checked attr), table, tableRow, tableHeader, tableCell,
image (+ src alt attrs, CDATA ignored), divider, callout

Inline elements inside <bdx:block>
-----------------------------------
<bdx:content>   CDATA text run — plain text or CDATA-escaped rich text
<bdx:link>      link to another doc; target = rel_path, anchor = block_id (optional)
<bdx:tag>       inline tag; name = lowercased tag name
<bdx:bold>      bold wrapper mark (contains <bdx:content>)
<bdx:italic>    italic wrapper mark
<bdx:underline> underline wrapper mark
<bdx:strike>    strikethrough wrapper mark
<bdx:code>      inline code wrapper mark
<bdx:highlight> highlight wrapper mark (optional color attribute)
<bdx:color>     text color wrapper mark (value="#xxxxxx")
<bdx:subscript>   subscript wrapper mark
<bdx:superscript> superscript wrapper mark

Block id
--------
Optional attribute ``id`` on <bdx:block>.  Format: ``b_[a-z0-9]{4}``.
Assigned lazily by bdx_serializer on first save.

Lenient parsing
---------------
Unknown block types, unknown attributes, and malformed XML sections are
skipped with a warning rather than raising exceptions.

This module provides:
    parse(xml_text)  -> ParsedBdx
    plaintext(xml_text) -> str   (for FTS indexing)
"""

from __future__ import annotations

import logging
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

BDX_NS = "https://pyclaego.local/bdx/v1"
_NS = f"{{{BDX_NS}}}"
_BLOCK_ID_RE = re.compile(r"^b_[a-z0-9]{4,}$")
_TAG_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _is_doc_id(s: str) -> bool:
    """Check if a string looks like a UUID v4 doc_id (not a filesystem path)."""
    if not s or len(s) != 36 or s.count('-') != 4:
        return False
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


@dataclass
class BdxLink:
    target: str        # rel_path of target doc (must end in .bdx)
    anchor: str        # block id in target, or "" if none
    display: str       # display text


@dataclass
class BdxBlock:
    block_id: str | None    # None if not yet assigned
    kind: str                  # heading/paragraph/code/…
    attrs: dict                # type-specific attrs (level, lang, src, …)
    text: str                  # concatenated plaintext of all content runs
    links: list[BdxLink]       # <bdx:link> elements found in this block
    tags: list[str]            # <bdx:tag> names found in this block


@dataclass
class BdxMeta:
    doc_id: str | None = None
    rel_path: str | None = None
    title: str | None = None
    created_at: int | None = None
    modified_at: int | None = None


@dataclass
class ParsedBdx:
    meta: BdxMeta = field(default_factory=BdxMeta)
    blocks: list[BdxBlock] = field(default_factory=list)

    # Aggregated across all blocks (for convenient indexing)
    @property
    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        result = []
        for b in self.blocks:
            for t in b.tags:
                if t not in seen:
                    seen.add(t)
                    result.append(t)
        return result

    @property
    def all_links(self) -> list[BdxLink]:
        """Deduplicated links (same target+anchor = one edge)."""
        seen: set[tuple[str, str]] = set()
        result = []
        for b in self.blocks:
            for lnk in b.links:
                key = (lnk.target, lnk.anchor)
                if key not in seen:
                    seen.add(key)
                    result.append(lnk)
        return result

    @property
    def plaintext(self) -> str:
        """All text runs joined with newlines — suitable for FTS indexing."""
        parts = []
        for b in self.blocks:
            if b.text.strip():
                parts.append(b.text)
        return "\n".join(parts)

    @property
    def title(self) -> str | None:
        """First heading block text, or meta.title."""
        if self.meta.title:
            return self.meta.title
        for b in self.blocks:
            if b.kind == "heading":
                t = b.text.strip()
                if t:
                    return t
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_bdx(xml_text: str) -> ParsedBdx:
    """Parse a .bdx XML document.  Returns ParsedBdx on any input (lenient)."""
    result = ParsedBdx()
    if not xml_text or not xml_text.strip():
        return result
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        _log.warning("bdx_parser: XML parse error — %s", exc)
        return result

    # Register the bdx: namespace for find() calls
    ns = {"bdx": BDX_NS}

    # ── meta ─────────────────────────────────────────────────────────────
    meta_el = root.find("bdx:meta", ns)
    if meta_el is not None:
        result.meta = _parse_meta(meta_el, ns)

    # ── body ─────────────────────────────────────────────────────────────
    body_el = root.find("bdx:body", ns)
    if body_el is not None:
        for block_el in body_el:
            local = _local(block_el.tag)
            if local != "block":
                continue
            block = _parse_block(block_el, ns)
            if block is not None:
                result.blocks.append(block)

    return result


def plaintext(xml_text: str) -> str:
    """Fast path — just extract plain text for FTS, skip structural parsing."""
    return parse_bdx(xml_text).plaintext


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Strip Clark-notation namespace prefix: {ns}local -> local."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _parse_meta(el: ET.Element, ns: dict) -> BdxMeta:
    def _text(tag: str) -> str | None:
        child = el.find(f"bdx:{tag}", ns)
        return (child.text or "").strip() or None if child is not None else None

    def _int(tag: str) -> int | None:
        v = _text(tag)
        try:
            return int(v) if v else None
        except (ValueError, TypeError):
            return None

    return BdxMeta(
        doc_id=_text("doc_id"),
        rel_path=_text("rel_path"),
        title=_text("title"),
        created_at=_int("created_at"),
        modified_at=_int("modified_at"),
    )


def _parse_block(el: ET.Element, ns: dict) -> BdxBlock | None:
    kind = el.get("type", "paragraph")
    block_id_raw = el.get("id")
    block_id = block_id_raw if (block_id_raw and _BLOCK_ID_RE.match(block_id_raw)) else None

    # Extra attrs
    attrs: dict = {}
    if kind == "heading":
        try:
            attrs["level"] = int(el.get("level", "1"))
        except ValueError:
            attrs["level"] = 1
    elif kind == "code":
        attrs["lang"] = el.get("lang", "")
    elif kind == "image":
        attrs["src"] = el.get("src", "")
        attrs["alt"] = el.get("alt", "")

    text_parts: list[str] = []
    links: list[BdxLink] = []
    tags: list[str] = []

    for child in el:
        local = _local(child.tag)
        if local == "content":
            text_parts.append(child.text or "")
        elif local == "link":
            target = child.get("target", "").strip()
            if not target:
                continue
            if not target.endswith(".bdx") and not _is_doc_id(target):
                target = target + ".bdx"
            # Path safety: reject absolute paths only
            # Relative paths with .. are allowed — vault.py normalizes them
            if target.startswith("/"):
                _log.warning("bdx_parser: rejected unsafe link target %r", target)
                continue
            anchor = child.get("anchor", "").strip()
            display = child.get("display", "").strip()
            links.append(BdxLink(target=target, anchor=anchor, display=display))
            # Include display text in full-text body
            if display:
                text_parts.append(display)
        elif local == "tag":
            name = child.get("name", "").strip().lower()
            if name and _TAG_NAME_RE.match(name):
                tags.append(name)
        else:
            # Recursively extract text from wrapper mark elements
            # (bold, italic, underline, strike, code, highlight, subscript,
            #  superscript, etc.) and unknown elements — for FTS indexing
            text_parts.append(_extract_text_recursive(child))

    return BdxBlock(
        block_id=block_id,
        kind=kind,
        attrs=attrs,
        text="".join(text_parts),
        links=links,
        tags=tags,
    )


def _extract_text_recursive(el: ET.Element) -> str:
    """Recursively extract text from an element and its children.

    Used for wrapper mark elements (bold, italic, etc.) where the text
    content is nested inside formatting elements.
    """
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        local = _local(child.tag)
        if local == "content":
            parts.append(child.text or "")
        elif local == "link":
            display = child.get("display", "").strip()
            if display:
                parts.append(display)
        else:
            parts.append(_extract_text_recursive(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


__all__ = ["BDX_NS", "BdxBlock", "BdxLink", "BdxMeta", "ParsedBdx", "parse_bdx", "plaintext"]
