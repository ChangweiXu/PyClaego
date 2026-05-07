"""Backward-compatibility shim — real implementation lives in pyclaego.note_system."""
from .....note_system.events import (
    AsyncListener,
    EventBus,
    VaultEvent,
    VaultEventType,
)

__all__ = ["AsyncListener", "EventBus", "VaultEvent", "VaultEventType"]
