#!/usr/bin/env python3
"""Bootstrap PyClaego — create ~/.pyclaego/ with config files and data directories.

Run once after cloning or installing:

    cd pyclaego/
    python bootstrap.py

The script is idempotent: existing files and directories are never overwritten
or removed.

Directory layout created
------------------------
~/.pyclaego/             — unified root (config + data)
  config.yaml            — copied from config.example.yaml (skip if exists)
  .config.d/
    llm.yaml             — LLM provider definitions (edit to add your API keys)
    tools.yaml           — tool enable/disable flags
    security.yaml        — security rule configuration
    session/             — per-session config examples
      session.config.example.yaml
      session_spawn.config.yaml
      session_soul6.config.yaml
      session_feishu.config.yaml
  .logs/                 — runtime log files
  .memory/soul_v5/       — SoulV5 memory store (MD files + SQLite index)
  .memory/soul_v6/       — SoulV6 memory store
  workspaces/            — per-session working directories
  .cache/                — misc runtime caches (e.g. feishu session map)
  skills/builtin/        — built-in skill files (copied from repo skills/builtin/)
  skills/session_created/ — session-created skill files (copied from repo skills/session_created/)
"""

import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Source (relative to this script) → Destination (relative to ~/.pyclaego/)
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent.resolve()
ROOT = Path.home() / ".pyclaego"

CONFIG_FILES: list[tuple[str, str]] = [
    # (src path relative to HERE,                   dest path relative to CONFIG_ROOT)
    ("config.example.yaml",                          "config.yaml"),
    (".config.d/llm.yaml",                           ".config.d/llm.yaml"),
    (".config.d/tools.yaml",                         ".config.d/tools.yaml"),
    (".config.d/security.yaml",                      ".config.d/security.yaml"),
    (".config.d/session/session.config.example.yaml",
     ".config.d/session/session.config.example.yaml"),
    (".config.d/session/session_spawn.config.yaml",  ".config.d/session/session_spawn.config.yaml"),
    (".config.d/session/session_soul6.config.yaml",  ".config.d/session/session_soul6.config.yaml"),
    (".config.d/session/session_feishu.config.yaml", ".config.d/session/session_feishu.config.yaml"),
]

DATA_DIRS: list[str] = [
    ".logs",
    ".memory/soul_v5",
    ".memory/soul_v6",
    "workspaces",
    ".cache",
]

SKILLS_SRC: str = "skills"


def _copy_files() -> None:
    missing_sources: list[str] = []

    for src_rel, dest_rel in CONFIG_FILES:
        src = HERE / src_rel
        dest = ROOT / dest_rel

        if not src.exists():
            missing_sources.append(src_rel)
            continue

        if dest.exists():
            print(f"  [skip]  {dest_rel}  (already exists)")
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  [copy]  {src_rel}  →  {dest_rel}")

    if missing_sources:
        print("\nWarning: the following source files were not found and were skipped:")
        for s in missing_sources:
            print(f"  {s}")
        print("Run bootstrap.py from the pyclaego/ project directory.")


def _patch_python_path() -> None:
    """Sync python_exec.python_path in the deployed tools.yaml to sys.executable."""
    tools_yaml = ROOT / ".config.d" / "tools.yaml"
    if not tools_yaml.exists():
        return

    content = tools_yaml.read_text(encoding="utf-8")
    replacement = f'  python_path: "{sys.executable}"  # synced by bootstrap.py'
    patched, count = re.subn(
        r'^[ \t]*python_path:.*$',
        replacement,
        content,
        flags=re.MULTILINE,
    )
    if count == 0:
        print("  [warn]  python_exec.python_path not found in tools.yaml — skipping")
        return
    if patched == content:
        print(f"  [skip]  python_exec.python_path already set to {sys.executable}")
        return
    tools_yaml.write_text(patched, encoding="utf-8")
    print(f"  [set]   python_exec.python_path = {sys.executable}")


def _create_data_dirs() -> None:
    for d in DATA_DIRS:
        target = ROOT / d
        if target.exists():
            print(f"  [skip]  .pyclaego/{d}/  (already exists)")
        else:
            target.mkdir(parents=True, exist_ok=True)
            print(f"  [mkdir] .pyclaego/{d}/")


def _copy_skills() -> None:
    src_root = HERE / SKILLS_SRC
    if not src_root.exists():
        print(f"  [warn]  skills/ directory not found at {src_root} — skipping")
        return

    for src_file in sorted(src_root.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(HERE)
        dest = ROOT / rel
        if dest.exists():
            print(f"  [skip]  {rel}  (already exists)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        print(f"  [copy]  {rel}")


def main() -> None:
    print("PyClaego Bootstrap")
    print(f"  Root : {ROOT}")
    print()

    print(f"Copying config files to {ROOT} ...")
    _copy_files()
    print()

    print("Patching tool configuration ...")
    _patch_python_path()
    print()

    print(f"Creating data directories under {ROOT} ...")
    _create_data_dirs()
    print()

    print(f"Copying skills to {ROOT}/skills/ ...")
    _copy_skills()

    print()
    print("Done.  Next steps:")
    print(f"  1. Edit {ROOT}/.config.d/llm.yaml  — add your API key(s)")
    print(f"  2. Edit {ROOT}/config.yaml         — set agent.type / context.type")
    print(f"  3. python core_server.py           — start the scheduler")
    print(f"  4. python tui_client.py            — connect the TUI client")


if __name__ == "__main__":
    main()
