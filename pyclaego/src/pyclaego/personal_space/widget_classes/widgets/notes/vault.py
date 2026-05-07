"""Backward-compatibility shim — real implementation lives in pyclaego.note_system."""
from .....note_system.vault import DocMeta, NoteVault

__all__ = ["DocMeta", "NoteVault"]
