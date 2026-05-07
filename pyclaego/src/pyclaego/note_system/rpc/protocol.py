"""JSON-RPC 2.0 protocol models for the note system service bus."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Wire-format models
# ---------------------------------------------------------------------------

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: Any = None


class JsonRpcResponse(BaseModel):
    """Successful response — must never include an error field."""
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    result: Any


class JsonRpcErrorResponse(BaseModel):
    """Error response — must never include a result field."""
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    error: JsonRpcError


class JsonRpcNotification(BaseModel):
    """Server-push message with no id (client never expects a reply)."""
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: Any = None


# ---------------------------------------------------------------------------
# Standard JSON-RPC 2.0 error codes
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700       # Invalid JSON received
INVALID_REQUEST = -32600   # Not a valid Request object
METHOD_NOT_FOUND = -32601  # Method does not exist
INVALID_PARAMS = -32602    # Invalid method parameters
INTERNAL_ERROR = -32603    # Internal JSON-RPC error

# Application-level error codes (server-defined range: -32000 to -32099)
APP_NOT_FOUND = -32001        # FileNotFoundError / doc not in index
APP_ALREADY_EXISTS = -32002   # FileExistsError
APP_PERMISSION_DENIED = -32003  # PermissionError / path traversal
APP_INVALID_PATH = -32004     # Path validation failure
APP_VAULT_NOT_OPEN = -32005   # Call made before vault.open


# ---------------------------------------------------------------------------
# Exception used inside the service / hub
# ---------------------------------------------------------------------------

class RpcException(Exception):
    """Raised inside service handlers to produce a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


__all__ = [
    "JsonRpcRequest",
    "JsonRpcError",
    "JsonRpcResponse",
    "JsonRpcErrorResponse",
    "JsonRpcNotification",
    "RpcException",
    # Error code constants
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "APP_NOT_FOUND",
    "APP_ALREADY_EXISTS",
    "APP_PERMISSION_DENIED",
    "APP_INVALID_PATH",
    "APP_VAULT_NOT_OPEN",
]
