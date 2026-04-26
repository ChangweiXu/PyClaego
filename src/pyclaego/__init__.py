"""PyClaego: a session-based AI agent framework with persistent memory,
WebSocket scheduler, and pluggable context handlers.
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("pyclaego")
except PackageNotFoundError:
    __version__ = "unknown"
