"""YAML frontmatter utilities for the notes widget.

Frontmatter block format (invisible to the user in the editor):

    ---
    doc_id: "uuid-v4"
    created_at: "2024-01-15T10:30:00Z"
    modified_at: "2024-01-16T08:00:00Z"
    rel_path: "examples/note-a.md"
    title: "Note A"
    ---

Rules:
- strip(content) → returns (frontmatter_dict, body_str)
- inject(body, meta) → returns full file content with frontmatter prepended
- Frontmatter is never shown to the user; always stripped before serving.
- On parse failure, returns ({}, original_content) — graceful degradation.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Matches the leading ---...--- block (non-greedy, DOTALL)
_FM_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def strip(content: str) -> tuple[dict[str, Any], str]:
    """Split file content into (frontmatter_dict, body).

    Returns ({}, content) on any parse failure.
    """
    m = _FM_RE.match(content)
    if not m:
        return {}, content
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return {}, content
    except yaml.YAMLError:
        return {}, content
    body = content[m.end():]
    return fm, body


def inject(body: str, meta: dict[str, Any]) -> str:
    """Prepend a YAML frontmatter block to body."""
    fm_str = yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=True)
    return f"---\n{fm_str}---\n{body}"


def extract_title(body: str) -> str | None:
    """Return the text of the first heading found in body, or None."""
    for line in body.splitlines():
        stripped = line.lstrip("#").strip()
        if line.startswith("#") and stripped:
            return stripped
    return None


__all__ = ["extract_title", "inject", "strip"]
