"""Markdown link / tag parser for the notes widget.

Supported syntax:
  [[#tagname]]              — tag reference; tag_name = "tagname"
  [[path/to/file.md]]       — doc link; no anchor
  [[path/to/file.md:section]]  — doc link with section anchor (stored as display_text)

Disambiguation rule:
  - Tag iff content starts with '#' AND contains no '/' before '#'
    and tag_name matches [a-z0-9_-]+ (after lowercasing).
  - Everything else is a doc link.

Tag names are normalised to lowercase and stripped.
Tag names must match [a-z0-9_-]; invalid names are silently skipped.

Doc link rel_path is the part before ':' (colon), stripped of '.md' suffix if absent
(we always normalise to have .md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_TAG_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass
class ParsedLinks:
    tags: list[str] = field(default_factory=list)        # normalised tag names
    doc_links: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, display_text)


def parse(body: str) -> ParsedLinks:
    """Extract all [[#tag]] and [[path:section]] references from body."""
    result = ParsedLinks()
    seen_tags: set[str] = set()
    seen_links: set[str] = set()

    for m in _WIKILINK_RE.finditer(body):
        content = m.group(1).strip()
        if not content:
            continue

        if content.startswith("#"):
            # Tag reference
            tag_raw = content[1:].strip().lower()
            if _TAG_NAME_RE.match(tag_raw) and tag_raw not in seen_tags:
                result.tags.append(tag_raw)
                seen_tags.add(tag_raw)
        else:
            # Doc link — split on first ':'
            if ":" in content:
                rel_path_raw, anchor = content.split(":", 1)
                rel_path_raw = rel_path_raw.strip()
                anchor = anchor.strip()
                display_text = anchor
            else:
                rel_path_raw = content.strip()
                display_text = ""

            # Normalise: ensure .md extension
            rel_path = rel_path_raw if rel_path_raw.endswith(".md") else rel_path_raw + ".md"
            # Sanitise: reject empty or absolute paths
            if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
                continue
            if rel_path not in seen_links:
                result.doc_links.append((rel_path, display_text))
                seen_links.add(rel_path)

    return result


__all__ = ["ParsedLinks", "parse"]
