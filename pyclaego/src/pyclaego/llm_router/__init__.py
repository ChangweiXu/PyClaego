"""LLM Router — local LLM forwarding proxy.

Accepts OpenAI / Anthropic / Gemini wire-format REST requests on localhost,
routes by the request's `model` field to a configured upstream (rewriting
only the model name, URL, and auth headers), and forwards the response
unchanged. Streaming and non-streaming are supported via separate handler
classes per protocol. Each call is dumped to a JSON file (credentials
masked) and a row is recorded in a SQLite stats DB.
"""

from .app import create_app
from .config import RouterConfig, load_router_config

__all__ = ["RouterConfig", "create_app", "load_router_config"]
