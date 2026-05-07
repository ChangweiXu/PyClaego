"""Backward-compatibility shim — real implementation lives in pyclaego.note_system."""
from .....note_system.frontmatter import extract_title, inject, strip

__all__ = ["extract_title", "inject", "strip"]
