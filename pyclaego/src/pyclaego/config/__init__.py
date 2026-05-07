"""配置模块"""

# ---------------------------------------------------------------------------
# Default path constants — single source of truth for all ~/.pyclaego defaults.
# Sub-systems import the constant they need; never hard-code these strings.
# Expanded to absolute paths at import time so callers get a real path string.
# ---------------------------------------------------------------------------
from pathlib import Path as _Path

from .json_loader import (
    ConfigIncludeError,
    load_json_file,
    load_json_str,
    resolve_tree,
)
from .manager import ConfigManager, get_config, get_session_config
from .personal_space_config import (
    PersonalSpaceConfigManager,
    get_ps_config,
    get_ps_widget_config,
)
from .resolver import deep_merge, resolve_layers

_PYCLAEGO_HOME = _Path.home() / ".pyclaego"

PYCLAEGO_DEFAULT_ROOT       = str(_PYCLAEGO_HOME)                       # project root
PYCLAEGO_DEFAULT_LOG_ROOT   = str(_PYCLAEGO_HOME / "logs")              # LogManager / RunningLog
PYCLAEGO_DEFAULT_LOGS_ROOT  = str(_PYCLAEGO_HOME / ".logs")             # SecurityExecutor audit records
PYCLAEGO_DEFAULT_CACHE_ROOT = str(_PYCLAEGO_HOME / ".cache")            # TaskArtifactStore / public_paths
PYCLAEGO_DEFAULT_SKILL_ROOT = str(_PYCLAEGO_HOME / "skills")            # Skills directory
PYCLAEGO_DEFAULT_WORKSPACES = str(_PYCLAEGO_HOME / "personal_spaces")   # personal space root

__all__ = [
    # public API — external modules should only import from this list
    'get_config',
    'get_session_config',
    'get_ps_config',
    'get_ps_widget_config',
    # utilities & helpers
    'deep_merge',
    'resolve_layers',
    'load_json_file',
    'load_json_str',
    'resolve_tree',
    'ConfigIncludeError',
    # path defaults
    'PYCLAEGO_DEFAULT_ROOT',
    'PYCLAEGO_DEFAULT_LOG_ROOT',
    'PYCLAEGO_DEFAULT_LOGS_ROOT',
    'PYCLAEGO_DEFAULT_CACHE_ROOT',
    'PYCLAEGO_DEFAULT_WORKSPACES',
    # kept importable for test introspection but NOT part of public API
    'ConfigManager',
    'PersonalSpaceConfigManager',
]
