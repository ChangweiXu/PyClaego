"""Workspace path containment rule.

Central defense-in-depth rule that constrains every tool's filesystem
footprint to a small set of allowed roots:

* the per-session **workspace** directory,
* (optionally) the per-session **project** directory declared in
  ``session_metadata.project_root`` of the session-scoped config, and
* a global **public path whitelist** shared by every session/agent
  (e.g. ``/tmp``, ``~/pyclaego/.cache``).

The rule asks each tool for its declared read/write paths via
``BaseTool.extract_paths`` instead of inspecting raw arg names, so it
stays consistent with whatever arg-grammar a tool uses.

Configuration example::

    rule_type: "workspace_path"
    rule_id: "subagent_workspace_restriction"
    enabled: true
    request_types: ["tool_call"]
    action: "deny"
    # If non-empty, only these tools are checked. Empty/omitted = all tools.
    restricted_tools: []
    # Allow read access (and optionally writes) into the project root
    # declared by the session's session_metadata.project_root.
    allow_project_root: true
    allow_project_writes: false
    # Globally allowed paths (read + write) for every session/agent.
    public_paths:
      - "/tmp"
      - "~/pyclaego/.cache"

Roots resolved per call:

* **Workspace** — ``session.session_workspace_root.<session_id>`` (if
  set), otherwise ``session.workspace_root/<session_id>``. Read from the
  global config.
* **Project** — ``session_metadata.project_root`` of the session-scoped
  config returned by :func:`get_session_config`. Sessions without this
  field have no project root.
* **Public paths** — ``public_paths`` on the rule itself.

Path strings supplied by tools may contain placeholders ``{{WORKSPACE}}``
and ``{{PROJECT}}``; both are expanded before containment is tested.
"""

from pathlib import Path
from typing import Any

from ...config import (
    PYCLAEGO_DEFAULT_CACHE_ROOT,
    PYCLAEGO_DEFAULT_SKILL_ROOT,
    PYCLAEGO_DEFAULT_WORKSPACES,
    get_config,
    get_session_config,
)
from ...logging import get_running_log
from ...tool.tool_manager import ToolManager
from ..base_rule import BaseSecurityRule

_rlog = get_running_log()

# Default public whitelist applied when ``public_paths`` is omitted from
# the rule config. These directories are always readable and writable.
_DEFAULT_PUBLIC_PATHS: tuple[str, ...] = (
    "/tmp",
    PYCLAEGO_DEFAULT_CACHE_ROOT,
    PYCLAEGO_DEFAULT_SKILL_ROOT,
)


class WorkspacePathRule(BaseSecurityRule):
    """Constrain every tool's read/write footprint to allowed roots."""

    def __init__(self, rule_config: dict[str, Any]) -> None:
        super().__init__(rule_config)

        # Empty list / missing key = "all tools".
        self.restricted_tools: list[str] = list(
            rule_config.get("restricted_tools", []) or []
        )
        self.allow_project_root: bool = bool(
            rule_config.get("allow_project_root", True)
        )
        self.allow_project_writes: bool = bool(
            rule_config.get("allow_project_writes", False)
        )

        # Public whitelist — applies to every session/agent (read + write).
        raw_public = rule_config.get("public_paths")
        if raw_public is None:
            raw_public = list(_DEFAULT_PUBLIC_PATHS)
        self._public_paths: list[Path] = []
        for entry in raw_public:
            if isinstance(entry, str) and entry.strip():
                try:
                    self._public_paths.append(Path(entry).expanduser().resolve())
                except (OSError, RuntimeError):
                    _rlog.warning(
                        "core_service",
                        f"[WorkspacePathRule] {self.rule_id}: ignoring "
                        f"unresolvable public_paths entry {entry!r}",
                    )

        cfg = get_config()
        session_cfg = cfg.get("session", {}) or {}
        self._workspace_root = Path(
            session_cfg.get("workspace_root", PYCLAEGO_DEFAULT_WORKSPACES)
        ).expanduser()
        self._session_workspace_map: dict[str, str] = (
            session_cfg.get("session_workspace_root") or {}
        )

        _rlog.info(
            "core_service",
            f"[WorkspacePathRule] init {self.rule_id}: "
            f"restricted_tools={self.restricted_tools or 'ALL'}, "
            f"workspace_root={self._workspace_root}, "
            f"allow_project_root={self.allow_project_root}, "
            f"allow_project_writes={self.allow_project_writes}, "
            f"public_paths={[str(p) for p in self._public_paths]}",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_workspace_path(self, session_id: str) -> Path:
        if session_id and session_id in self._session_workspace_map:
            return Path(self._session_workspace_map[session_id]).expanduser()
        return self._workspace_root / (session_id or "_default")

    def _get_project_path(self, session_id: str) -> Path | None:
        """Resolve the project root for ``session_id`` from session config.

        Reads ``session_metadata.project_root`` from the session-scoped
        config returned by :func:`get_session_config`. Sessions that do
        not declare one return ``None``.
        """
        if not session_id:
            return None
        try:
            scfg = get_session_config(session_id)
            metadata = scfg.get("session_metadata", {}) or {}
        except Exception as e:
            _rlog.warning(
                "core_service",
                f"[WorkspacePathRule] {self.rule_id}: failed to load "
                f"session config for '{session_id}': {e}",
            )
            return None
        raw = metadata.get("project_root")
        if not raw:
            return None
        try:
            return Path(str(raw)).expanduser()
        except (OSError, RuntimeError) as e:
            _rlog.warning(
                "core_service",
                f"[WorkspacePathRule] {self.rule_id}: invalid project_root "
                f"in session_metadata for '{session_id}': {e}",
            )
            return None

    @staticmethod
    def _expand_placeholders(
        path_str: str, workspace: Path, project: Path | None
    ) -> str:
        if "{{WORKSPACE}}" in path_str:
            path_str = path_str.replace("{{WORKSPACE}}", str(workspace))
        if "{{PROJECT}}" in path_str and project is not None:
            path_str = path_str.replace("{{PROJECT}}", str(project))
        return path_str

    @staticmethod
    def _is_inside(path: Path, root: Path) -> bool:
        if path == root:
            return True
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _check_path(
        self,
        raw: str,
        kind: str,
        workspace: Path,
        project: Path | None,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a single declared path."""
        expanded = self._expand_placeholders(raw, workspace, project)
        try:
            resolved = Path(expanded).expanduser().resolve()
        except (OSError, RuntimeError) as e:
            return False, f"unresolvable path {raw!r}: {e}"

        # Always resolve roots so symlink-equivalent paths (e.g. /tmp on
        # macOS -> /private/tmp) compare correctly. ``resolve()`` does not
        # require the path to exist.
        ws_resolved = workspace.expanduser().resolve()
        if self._is_inside(resolved, ws_resolved):
            return True, ""

        # Public whitelist — read and write are both allowed for every session.
        for pub in self._public_paths:
            if self._is_inside(resolved, pub):
                return True, ""

        if self.allow_project_root and project is not None:
            proj_resolved = project.expanduser().resolve()
            if self._is_inside(resolved, proj_resolved):
                if kind == "read" or self.allow_project_writes:
                    return True, ""
                return False, (
                    f"{kind} into project root is not allowed "
                    f"(allow_project_writes=false): {resolved}"
                )

        return False, (
            f"{kind} path escapes allowed roots "
            f"(workspace={ws_resolved}"
            + (f", project={project}" if project else "")
            + (
                f", public={[str(p) for p in self._public_paths]}"
                if self._public_paths
                else ""
            )
            + f"): {resolved}"
        )

    # ------------------------------------------------------------------
    # BaseSecurityRule interface
    # ------------------------------------------------------------------

    async def matches(self, request: dict[str, Any]) -> bool:
        if not self.is_enabled():
            return False
        if not self.applies_to_request_type(request.get("type", "")):
            return False

        tool_name = request.get("tool_name", "")
        if not tool_name:
            return False
        if self.restricted_tools and tool_name not in self.restricted_tools:
            return False

        tool = ToolManager.get_instance().get_tool(tool_name)
        if tool is None:
            # No registered tool — nothing for this rule to enforce.
            return False

        tool_args = request.get("tool_args", {}) or {}
        try:
            paths = tool.extract_paths(tool_args) or {}
        except Exception as e:
            _rlog.warning(
                "core_service",
                f"[WorkspacePathRule] {self.rule_id}: extract_paths failed for "
                f"'{tool_name}': {e}",
            )
            return False

        reads = [
            p for p in (paths.get("read") or []) if isinstance(p, str) and p.strip()
        ]
        writes = [
            p for p in (paths.get("write") or []) if isinstance(p, str) and p.strip()
        ]
        if not reads and not writes:
            return False

        session_id = request.get("session_id", "") or ""
        workspace = self._get_workspace_path(session_id)
        project = self._get_project_path(session_id) if self.allow_project_root else None

        for raw in writes:
            ok, reason = self._check_path(raw, "write", workspace, project)
            if not ok:
                _rlog.warning(
                    "core_service",
                    f"[WorkspacePathRule] {self.rule_id} matched: tool "
                    f"'{tool_name}' (session={session_id}): {reason}",
                )
                return True
        for raw in reads:
            ok, reason = self._check_path(raw, "read", workspace, project)
            if not ok:
                _rlog.warning(
                    "core_service",
                    f"[WorkspacePathRule] {self.rule_id} matched: tool "
                    f"'{tool_name}' (session={session_id}): {reason}",
                )
                return True

        return False
