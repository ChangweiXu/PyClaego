"""Mask credentials from headers / bodies / query params before dumping.

Designed to be conservative: anything matching a configured key name (case
-insensitive) is replaced by a fixed redaction marker, regardless of value
shape. Walks nested dicts/lists. Strings that *look like* an API key are
NOT auto-detected — only declared keys are masked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

REDACTED = "***REDACTED***"


def _mask_value(_v: Any) -> str:
    return REDACTED


def mask_headers(
    headers: Mapping[str, str], mask_keys: Iterable[str]
) -> dict[str, str]:
    keys_lower = {k.lower() for k in mask_keys}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in keys_lower:
            out[k] = REDACTED
        elif k.lower() == "authorization":
            # Always mask Authorization, regardless of mask_keys spelling.
            out[k] = REDACTED
        else:
            out[k] = v
    return out


def mask_query_params(
    params: Mapping[str, Any], mask_keys: Iterable[str]
) -> dict[str, Any]:
    keys_lower = {k.lower() for k in mask_keys}
    out: dict[str, Any] = {}
    for k, v in params.items():
        out[k] = REDACTED if k.lower() in keys_lower else v
    return out


def mask_body(body: Any, mask_keys: Iterable[str]) -> Any:
    """Recursively walk a JSON-shaped body, redacting any key in mask_keys.

    Returns a deep copy. Non-dict/list values are returned unchanged.
    """
    keys_lower = {k.lower() for k in mask_keys}
    return _walk(deepcopy(body), keys_lower)


def _walk(node: Any, keys_lower: set[str]) -> Any:
    if isinstance(node, dict):
        for k in list(node.keys()):
            if isinstance(k, str) and k.lower() in keys_lower:
                node[k] = REDACTED
            else:
                node[k] = _walk(node[k], keys_lower)
        return node
    if isinstance(node, list):
        for i, item in enumerate(node):
            node[i] = _walk(item, keys_lower)
        return node
    return node
