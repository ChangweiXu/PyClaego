"""Backward-compatibility shim — real implementation lives in pyclaego.note_system."""
from .....note_system.parser import ParsedLinks, parse

__all__ = ["ParsedLinks", "parse"]
