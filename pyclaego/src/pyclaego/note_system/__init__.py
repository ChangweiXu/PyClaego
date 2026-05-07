"""Note System — process-level vault management for the notes widget.

Public API:
  NoteSystemManager  — singleton vault registry (acquire / release / get)
  NoteVault          — per-root vault (DB, watcher, EventBus)
  VaultEvent         — event dataclass published on every CRUD op
  EventBus           — async fan-out pub/sub
  BDX_NS             — XML namespace constant
  NoteRpcHub         — JSON-RPC 2.0 WebSocket hub (one instance per WS connection)
"""

from .bdx_meta import BdxMeta
from .bdx_parser import BDX_NS, ParsedBdx, parse_bdx
from .events import EventBus, VaultEvent
from .manager import NoteSystemManager
from .rpc import NoteRpcHub
from .vault import NoteVault

__all__ = [
    "BDX_NS",
    "BdxMeta",
    "EventBus",
    "NoteRpcHub",
    "NoteSystemManager",
    "NoteVault",
    "ParsedBdx",
    "VaultEvent",
    "parse_bdx",
]
