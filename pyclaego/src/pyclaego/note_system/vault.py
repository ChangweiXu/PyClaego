"""NoteVault — the single managing class for the notes widget.

NoteVault is the exclusive authority over:
  - All .bdx file I/O (path traversal protection)
  - The SQLite index (docs, tags, doc_tags, doc_links, doc_blocks, docs_fts)
  - A watchfiles watcher task for real-time FS sync
  - An EventBus for pushing VaultEvents to subscribers (WebSocket handler, etc.)

Lifecycle:
  startup()  → bootstrap_db → seed_examples (if empty) → reconcile → _watch_loop
  shutdown() → cancel watcher → close db

File format: .bdx XML (see bdx_parser.py for the format spec).

All write operations are:
  1. Written to disk (atomic via Path.write_text)
  2. Followed immediately by index update
  3. Followed by EventBus.publish()

External changes (files edited/created/deleted outside the API) are caught by
the watcher and reconciled automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite

from ..logging import get_running_log
from . import bdx_meta as bm
from .bdx_meta import BdxMeta
from .bdx_parser import parse_bdx
from .bdx_serializer import assign_block_ids, empty_doc
from .events import EventBus, VaultEvent
from .tiptap_json_converter import tiptap_json_to_xml, xml_to_tiptap_json

_rlog = get_running_log()

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"
_SEED_DIR = Path(__file__).parent / "seed_docs"

_EXT = ".bdx"


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

class DocMeta:
    __slots__ = ("created_at", "doc_id", "modified_at", "rel_path", "stub", "title")

    def __init__(
        self,
        doc_id: str,
        rel_path: str,
        title: str | None,
        created_at: int,
        modified_at: int,
        stub: bool = False,
    ) -> None:
        self.doc_id = doc_id
        self.rel_path = rel_path
        self.title = title
        self.created_at = created_at
        self.modified_at = modified_at
        self.stub = stub

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "rel_path": self.rel_path,
            "title": self.title,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "stub": self.stub,
        }


# ---------------------------------------------------------------------------
# Link resolution helpers — resolve relative paths to doc IDs
# ---------------------------------------------------------------------------

_LINK_TARGET_RE = re.compile(r'(<bdx:link\s+([^>]*?)target=")([^"]*)(")', re.DOTALL)


def _is_doc_id(s: str) -> bool:
    """Check if a string looks like a UUID v4 doc_id (not a filesystem path)."""
    if not s:
        return False
    # UUID v4: 8-4-4-4-12 hex digits with dashes
    if len(s) == 36 and s.count('-') == 4:
        try:
            uuid.UUID(s)
            return True
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# NoteVault
# ---------------------------------------------------------------------------

class NoteVault:
    def __init__(self, doc_root: str | Path) -> None:
        self._root = Path(doc_root).expanduser().resolve()
        self._db_path = self._root / "project.db"
        self._db: aiosqlite.Connection | None = None
        self._bus = EventBus()
        self._watcher_task: asyncio.Task | None = None
        self._started = False
        # Paths whose watcher events should be suppressed (our own writes).
        self._suppress_watcher: set[str] = set()
        # Backup: file mtimes recorded right after an API write.  When the
        # watchfiles event arrives later (FSEvents latency), _handle_fs_change
        # can compare the on-disk mtime against this record and skip if it
        # matches – avoiding a spurious "changed externally" warning.
        self._recent_write_mtimes: dict[str, int] = {}

    # ── Public ────────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    @property
    def bus(self) -> EventBus:
        return self._bus

    # ── Watcher suppression ─────────────────────────────────────────────

    @contextlib.contextmanager
    def _suppress_watcher_for(self, rel_paths: Iterable[str]):
        """Context manager that suppresses watcher events for the given paths.

        Use this around any file write that should not trigger the
        watchfiles loop to re-process the same change.
        """
        self._suppress_watcher.update(rel_paths)
        try:
            yield
        finally:
            self._suppress_watcher.difference_update(rel_paths)

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def startup(self) -> None:
        if self._started:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._bootstrap_db()
        if self._is_empty():
            await self._seed_examples()
        await self._reconcile()
        self._watcher_task = asyncio.create_task(
            self._watch_loop(), name=f"notes-watcher:{self._root.name}"
        )
        self._started = True
        _rlog.info("widget_notes", f"[NoteVault] started at {self._root}")

    async def shutdown(self) -> None:
        if self._watcher_task:
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watcher_task = None
        if self._db:
            await self._db.close()
            self._db = None
        self._started = False

    # ── File tree ─────────────────────────────────────────────────────────

    def file_tree(self) -> list[dict[str, Any]]:
        """Return a nested list representing the directory tree under doc_root."""
        return self._build_tree(self._root, self._root)

    def _build_tree(self, path: Path, base: Path) -> list[dict[str, Any]]:
        entries = []
        try:
            items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return entries
        for item in items:
            if item.name.startswith(".") or item.name == "project.db":
                continue
            rel = item.relative_to(base).as_posix()
            if item.is_dir():
                entries.append({
                    "type": "dir",
                    "name": item.name,
                    "rel_path": rel,
                    "children": self._build_tree(item, base),
                })
            elif item.suffix == _EXT:
                entries.append({"type": "file", "name": item.name, "rel_path": rel})
        return entries

    # ── File CRUD ─────────────────────────────────────────────────────────

    async def read(self, rel_path: str) -> str | None:
        """Read raw .bdx XML content (including meta). Returns None if not found."""
        path = self._safe_path(rel_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    async def write(self, rel_path: str, xml_body: str) -> DocMeta:
        """Write .bdx XML, inject/refresh meta, update index, emit event."""
        path = self._safe_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        now_ms = _now_ms()

        existing_meta = await self._get_doc_by_path(rel_path)
        if existing_meta is not None:
            doc_id = existing_meta.doc_id
            created_at = existing_meta.created_at
        else:
            parsed_meta, _ = bm.strip_meta(xml_body)
            doc_id = parsed_meta.doc_id or str(uuid.uuid4())
            created_at = parsed_meta.created_at or now_ms

        xml_body = xml_body.strip()
        if not xml_body or not xml_body.startswith("<"):
            bdx_meta_obj = BdxMeta(
                doc_id=doc_id, rel_path=rel_path, title=None,
                created_at=created_at, modified_at=now_ms,
            )
            xml_body = empty_doc(bdx_meta_obj)

        xml_body, _ = assign_block_ids(xml_body)

        # Resolve all <bdx:link> targets to doc IDs
        xml_body, _ = await self._resolve_links_to_doc_ids(xml_body, rel_path)

        title = bm.extract_title(xml_body) or (existing_meta.title if existing_meta else None)

        bdx_meta_obj = BdxMeta(
            doc_id=doc_id, rel_path=rel_path, title=title,
            created_at=created_at, modified_at=now_ms,
        )
        content = bm.inject_meta(xml_body, bdx_meta_obj)
        with self._suppress_watcher_for([rel_path]):
            path.write_text(content, encoding="utf-8")
            # Record mtime so the watcher can recognise this as our own write
            # even after the suppression context exits (FSEvents latency).
            self._recent_write_mtimes[rel_path] = path.stat().st_mtime_ns

            doc_meta = DocMeta(
                doc_id=doc_id, rel_path=rel_path, title=title,
                created_at=created_at, modified_at=now_ms,
            )
            await self._index_doc(doc_meta, content)
            await self._db.commit()  # type: ignore[union-attr]
            await self._bus.publish(VaultEvent(
                type="modified",
                rel_path=rel_path,
                doc_id=doc_id,
                title=title,
                modified_at=now_ms,
            ))
        return doc_meta

    async def read_json(self, rel_path: str) -> dict | None:
        """Read .bdx XML, convert to Tiptap JSON. Returns None if not found."""
        xml_content = await self.read(rel_path)
        if xml_content is None:
            return None
        return xml_to_tiptap_json(xml_content, from_path=rel_path)

    async def write_json(self, rel_path: str, tiptap_json: dict) -> DocMeta:
        """Accept Tiptap JSON from frontend, convert to BDX XML, delegate to write()."""
        # Read existing XML to preserve <bdx:meta>
        existing_xml = await self.read(rel_path)
        xml_body = tiptap_json_to_xml(tiptap_json, existing_xml=existing_xml)
        return await self.write(rel_path, xml_body)

    async def create(self, rel_path: str) -> DocMeta:
        """Create an empty .bdx file. Returns DocMeta."""
        path = self._safe_path(rel_path)
        if path.exists():
            raise FileExistsError(f"{rel_path} already exists")
        return await self.write(rel_path, "")

    async def delete(self, rel_path: str) -> None:
        path = self._safe_path(rel_path)
        meta = await self._get_doc_by_path(rel_path)
        if path.exists():
            path.unlink()
        if meta:
            await self._remove_doc(meta.doc_id, rel_path)
            await self._db.commit()  # type: ignore[union-attr]
        await self._bus.publish(VaultEvent(
            type="deleted",
            rel_path=rel_path,
            doc_id=meta.doc_id if meta else None,
        ))

    async def rename(self, old_path: str, new_path: str) -> DocMeta:
        src = self._safe_path(old_path)
        dst = self._safe_path(new_path)
        if not src.exists():
            raise FileNotFoundError(old_path)
        if dst.exists():
            raise FileExistsError(new_path)
        dst.parent.mkdir(parents=True, exist_ok=True)

        raw = src.read_text(encoding="utf-8")
        parsed_meta, _ = bm.strip_meta(raw)
        now_ms = _now_ms()
        doc_id = parsed_meta.doc_id or str(uuid.uuid4())
        created_at = parsed_meta.created_at or now_ms
        title = bm.extract_title(raw) or parsed_meta.title

        updated_meta = BdxMeta(
            doc_id=doc_id, rel_path=new_path, title=title,
            created_at=created_at, modified_at=now_ms,
        )
        updated_content = bm.inject_meta(raw, updated_meta)
        with self._suppress_watcher_for([new_path, old_path]):
            dst.write_text(updated_content, encoding="utf-8")
            src.unlink()

        old_db_meta = await self._get_doc_by_path(old_path)
        if old_db_meta:
            await self._remove_doc(old_db_meta.doc_id, old_path)

        doc_meta = DocMeta(
            doc_id=doc_id, rel_path=new_path, title=title,
            created_at=created_at, modified_at=now_ms,
        )
        await self._index_doc(doc_meta, updated_content)
        await self._db.commit()  # type: ignore[union-attr]

        await self._bus.publish(VaultEvent(
            type="renamed",
            rel_path=new_path,
            old_path=old_path,
            doc_id=doc_id,
            title=title,
        ))
        return doc_meta

    # ── Query ─────────────────────────────────────────────────────────────

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search. Returns list of {doc_id, rel_path, title, snippet}."""
        assert self._db is not None
        # Escape FTS5 special chars
        q = query.replace('"', '""')
        sql = """
            SELECT d.doc_id, d.rel_path, d.title,
                   snippet(docs_fts, 2, '<mark>', '</mark>', '…', 32) AS snippet
            FROM docs_fts
            JOIN docs d USING (doc_id)
            WHERE docs_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        try:
            async with self._db.execute(sql, (f'"{q}"', limit)) as cur:
                rows = await cur.fetchall()
        except Exception:
            # Fallback to plain LIKE if FTS query is invalid
            async with self._db.execute(
                "SELECT doc_id, rel_path, title, '' FROM docs WHERE title LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"doc_id": r[0], "rel_path": r[1], "title": r[2], "snippet": r[3]}
            for r in rows
        ]

    async def graph(self) -> dict[str, Any]:
        assert self._db is not None
        async with self._db.execute("SELECT doc_id, rel_path, title FROM docs") as cur:
            doc_rows = await cur.fetchall()
        async with self._db.execute(
            "SELECT DISTINCT source_id, target_id, display_text FROM doc_links"
        ) as cur:
            edge_rows = await cur.fetchall()
        nodes = [
            {"id": r[0], "label": r[2] or r[1], "rel_path": r[1]}
            for r in doc_rows
        ]
        edges = [
            {"source": r[0], "target": r[1], "display_text": r[2] or ""}
            for r in edge_rows
        ]
        return {"nodes": nodes, "edges": edges}

    async def backlinks(self, rel_path: str) -> list[dict[str, Any]]:
        """Return all docs that link *to* rel_path, with snippet and optional block_anchor."""
        assert self._db is not None
        target = await self._get_doc_by_path(rel_path)
        if target is None:
            return []
        sql = """
            SELECT d.doc_id, d.rel_path, d.title,
                   dl.block_anchor, dl.display_text
            FROM doc_links dl
            JOIN docs d ON d.doc_id = dl.source_id
            WHERE dl.target_id = ?
            ORDER BY d.modified_at DESC
        """
        async with self._db.execute(sql, (target.doc_id,)) as cur:
            rows = await cur.fetchall()
        return [
            {
                "doc_id": r[0],
                "rel_path": r[1],
                "title": r[2],
                "block_anchor": r[3],
                "snippet": r[4] or "",
            }
            for r in rows
        ]

    async def resolve_link(
        self, from_path: str, target: str, anchor: str = ""
    ) -> dict[str, Any]:
        """Resolve a link target relative to from_path's directory.

        Returns {rel_path, doc_id, block_id, exists}.
        """
        resolved_path = self._resolve_rel_path(from_path, target)
        target_meta = await self._get_doc_by_path(resolved_path)
        return {
            "rel_path": resolved_path,
            "doc_id": target_meta.doc_id if target_meta else None,
            "block_id": anchor,
            "exists": target_meta is not None,
        }

    async def resolve_doc_id(self, doc_id: str) -> dict[str, Any] | None:
        """Look up a document by its doc_id. Returns {doc_id, rel_path, title} or None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT doc_id, rel_path, title FROM docs WHERE doc_id = ?", (doc_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"doc_id": row[0], "rel_path": row[1], "title": row[2]}

    async def all_tags(self) -> list[dict[str, Any]]:
        """Return all tags with doc count."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT t.tag_id, t.tag_name, COUNT(dt.doc_id) AS cnt "
            "FROM tags t LEFT JOIN doc_tags dt USING (tag_id) "
            "GROUP BY t.tag_id ORDER BY cnt DESC, t.tag_name"
        ) as cur:
            rows = await cur.fetchall()
        return [{"tag_id": r[0], "tag_name": r[1], "doc_count": r[2]} for r in rows]

    async def docs_by_tag(self, tag_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT d.doc_id, d.rel_path, d.title FROM docs d "
            "JOIN doc_tags dt ON d.doc_id = dt.doc_id WHERE dt.tag_id = ?",
            (tag_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"doc_id": r[0], "rel_path": r[1], "title": r[2]} for r in rows]

    async def autocomplete(self, prefix: str, kind: str) -> dict[str, Any]:
        """Return completion candidates (tags or link paths) matching prefix, sorted lexicographically."""
        assert self._db is not None
        limit = 20

        if kind == "tag":
            async with self._db.execute(
                "SELECT tag_name FROM tags WHERE tag_name LIKE ? ORDER BY tag_name LIMIT ?",
                (f"{prefix}%", limit),
            ) as cur:
                rows = await cur.fetchall()
            return {
                "prefix": prefix,
                "kind": "tag",
                "candidates": [{"value": r[0], "label": f"#{r[0]}"} for r in rows],
            }
        elif kind == "link":
            # Strip leading / for DB query (DB stores relative paths), but
            # return absolute paths so resolve_link always receives unambiguous input.
            search = prefix.lstrip('/')
            async with self._db.execute(
                "SELECT rel_path, title FROM docs WHERE rel_path LIKE ? ORDER BY rel_path LIMIT ?",
                (f"{search}%", limit),
            ) as cur:
                rows = await cur.fetchall()
            return {
                "prefix": prefix,
                "kind": "link",
                "candidates": [{"value": f"/{r[0]}", "label": r[1] or r[0]} for r in rows],
            }
        else:
            raise ValueError(f"Unknown autocomplete kind: {kind!r}")

    async def get_doc_meta(self, rel_path: str) -> dict[str, Any] | None:
        meta = await self._get_doc_by_path(rel_path)
        return meta.to_dict() if meta else None

    # ── Watcher ───────────────────────────────────────────────────────────

    async def _watch_loop(self) -> None:
        try:
            from watchfiles import Change, awatch  # type: ignore[import]
        except ImportError:
            _rlog.exception("widget_notes", "[NoteVault] watchfiles not installed; external changes won't be detected")
            return
        try:
            async for changes in awatch(str(self._root)):
                for change_type, path_str in changes:
                    path = Path(path_str)
                    if path.suffix != _EXT:
                        continue
                    try:
                        rel_path = path.relative_to(self._root).as_posix()
                    except ValueError:
                        continue
                    await self._handle_fs_change(change_type, rel_path, path)
        except asyncio.CancelledError:
            pass
        except Exception:
            _rlog.exception("widget_notes", "[NoteVault] watcher error")

    async def _handle_fs_change(self, change_type: Any, rel_path: str, path: Path) -> None:
        from watchfiles import Change  # type: ignore[import]
        # Suppress watcher echo of our own writes (via _suppress_watcher_for).
        if rel_path in self._suppress_watcher:
            return
        # Backup check: compare on-disk mtime against the mtime recorded
        # right after our API write.  watchfiles (FSEvents on macOS) can
        # deliver events with enough latency that the _suppress_watcher
        # context has already exited; the mtime match catches this case.
        recorded_mtime = self._recent_write_mtimes.get(rel_path)
        if recorded_mtime is not None and path.exists():
            if path.stat().st_mtime_ns == recorded_mtime:
                self._recent_write_mtimes.pop(rel_path, None)
                return
            else:
                # File was touched after our write — clean up stale entry.
                self._recent_write_mtimes.pop(rel_path, None)
        if change_type == Change.deleted:
            meta = await self._get_doc_by_path(rel_path)
            if meta:
                await self._remove_doc(meta.doc_id, rel_path)
                await self._db.commit()  # type: ignore[union-attr]
            await self._bus.publish(VaultEvent(type="deleted", rel_path=rel_path,
                                               doc_id=meta.doc_id if meta else None))
        elif change_type in (Change.added, Change.modified):
            if not path.exists():
                return
            raw = path.read_text(encoding="utf-8")
            parsed_meta, _ = bm.strip_meta(raw)
            existing_db = await self._get_doc_by_path(rel_path)
            now_ms = _now_ms()
            doc_id = (existing_db.doc_id if existing_db else None) or parsed_meta.doc_id or str(uuid.uuid4())
            created_at = (existing_db.created_at if existing_db else None) or parsed_meta.created_at or now_ms
            title = bm.extract_title(raw) or parsed_meta.title
            if not parsed_meta.doc_id:
                new_meta_obj = BdxMeta(doc_id=doc_id, rel_path=rel_path, title=title,
                                       created_at=created_at, modified_at=now_ms)
                raw = bm.inject_meta(raw, new_meta_obj)
            # Resolve link targets to doc IDs
            raw, _ = await self._resolve_links_to_doc_ids(raw, rel_path)
            # Persist if we changed anything
            if not parsed_meta.doc_id:
                with self._suppress_watcher_for([rel_path]):
                    path.write_text(raw, encoding="utf-8")
            new_doc = DocMeta(doc_id=doc_id, rel_path=rel_path, title=title,
                              created_at=created_at, modified_at=now_ms)
            await self._index_doc(new_doc, raw)
            await self._db.commit()  # type: ignore[union-attr]
            evt_type = "created" if change_type == Change.added else "modified"
            await self._bus.publish(VaultEvent(type=evt_type, rel_path=rel_path,
                                               doc_id=doc_id, title=title, modified_at=now_ms))

    # ── Startup helpers ───────────────────────────────────────────────────

    async def _bootstrap_db(self) -> None:
        schema = _SCHEMA_FILE.read_text(encoding="utf-8")
        await self._db.executescript(schema)  # type: ignore[union-attr]
        await self._db.commit()  # type: ignore[union-attr]

    def _is_empty(self) -> bool:
        return not any(self._root.rglob(f"*{_EXT}"))

    async def _seed_examples(self) -> None:
        """Copy seed documents into the vault, assign doc IDs, and resolve links.

        Two-pass approach:
        1. Copy all files, assign/generate doc IDs, write meta, index them.
        2. For each file, resolve relative link targets → doc IDs, rewrite, re-index.
        """
        if not _SEED_DIR.exists():
            return

        seed_files: list[tuple[str, Path]] = []
        for src in _SEED_DIR.rglob(f"*{_EXT}"):
            rel = src.relative_to(_SEED_DIR).as_posix()
            dst = self._root / rel
            if not dst.exists():
                seed_files.append((rel, src))

        if not seed_files:
            return

        # ── Pass 1: copy, assign IDs, index ──
        for rel, src in seed_files:
            dst = self._root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            raw = src.read_text(encoding="utf-8")
            doc_id = str(uuid.uuid4())
            now_ms = _now_ms()
            parsed_meta, _ = bm.strip_meta(raw)
            title = bm.extract_title(raw) or parsed_meta.title

            bdx_meta_obj = BdxMeta(
                doc_id=doc_id, rel_path=rel, title=title,
                created_at=now_ms, modified_at=now_ms,
            )
            content = bm.inject_meta(raw, bdx_meta_obj)
            content, _ = assign_block_ids(content)
            dst.write_text(content, encoding="utf-8")

            doc_meta = DocMeta(
                doc_id=doc_id, rel_path=rel, title=title,
                created_at=now_ms, modified_at=now_ms,
            )
            await self._index_doc(doc_meta, content)
        await self._db.commit()  # type: ignore[union-attr]

        # ── Pass 2: resolve links to doc IDs, rewrite, re-index ──
        for rel, _ in seed_files:
            dst = self._root / rel
            raw = dst.read_text(encoding="utf-8")
            resolved, changed = await self._resolve_links_to_doc_ids(raw, rel)
            if changed:
                with self._suppress_watcher_for([rel]):
                    dst.write_text(resolved, encoding="utf-8")
                doc_meta = await self._get_doc_by_path(rel)
                if doc_meta:
                    await self._index_doc(doc_meta, resolved)
        await self._db.commit()  # type: ignore[union-attr]

    async def _reconcile(self) -> None:
        """Diff DB vs filesystem; bring index up to date."""
        assert self._db is not None
        async with self._db.execute("SELECT rel_path, doc_id, modified_at FROM docs") as cur:
            db_rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}

        disk_files: dict[str, Path] = {}
        for path in self._root.rglob(f"*{_EXT}"):
            rel = path.relative_to(self._root).as_posix()
            disk_files[rel] = path

        for rel, path in disk_files.items():
            mtime_ms = int(path.stat().st_mtime * 1000)
            if rel not in db_rows or mtime_ms > db_rows[rel][1]:
                raw = path.read_text(encoding="utf-8")
                parsed_meta, _ = bm.strip_meta(raw)
                db_doc_id = db_rows.get(rel, (None,))[0]
                doc_id = db_doc_id or parsed_meta.doc_id or str(uuid.uuid4())
                ctime_ms = parsed_meta.created_at or int(path.stat().st_ctime * 1000)
                title = bm.extract_title(raw) or parsed_meta.title
                meta = DocMeta(doc_id=doc_id, rel_path=rel, title=title,
                               created_at=ctime_ms, modified_at=mtime_ms)
                if not parsed_meta.doc_id:
                    updated = BdxMeta(doc_id=doc_id, rel_path=rel, title=title,
                                      created_at=ctime_ms, modified_at=mtime_ms)
                    raw = bm.inject_meta(raw, updated)
                # Resolve link targets to doc IDs
                raw, link_changed = await self._resolve_links_to_doc_ids(raw, rel)
                if not parsed_meta.doc_id or link_changed:
                    with self._suppress_watcher_for([rel]):
                        path.write_text(raw, encoding="utf-8")
                await self._index_doc(meta, raw)

        for rel, (doc_id, _) in db_rows.items():
            if rel not in disk_files:
                await self._remove_doc(doc_id, rel)

        await self._db.commit()  # type: ignore[union-attr]
        await self._bus.publish(VaultEvent(type="index_ready", rel_path=""))

    # ── Index helpers ─────────────────────────────────────────────────────

    async def _index_doc(self, meta: DocMeta, xml_content: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO docs (doc_id, rel_path, title, created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (meta.doc_id, meta.rel_path, meta.title, meta.created_at, meta.modified_at),
        )

        # Parse XML
        parsed = parse_bdx(xml_content)

        # FTS — plain text, not raw XML
        await self._db.execute("DELETE FROM docs_fts WHERE doc_id = ?", (meta.doc_id,))
        await self._db.execute(
            "INSERT INTO docs_fts (doc_id, title, body) VALUES (?, ?, ?)",
            (meta.doc_id, meta.title or "", parsed.plaintext),
        )

        # Tags
        await self._db.execute("DELETE FROM doc_tags WHERE doc_id = ?", (meta.doc_id,))
        for tag_name in parsed.all_tags:
            await self._db.execute(
                "INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (tag_name,)
            )
            async with self._db.execute(
                "SELECT tag_id FROM tags WHERE tag_name = ?", (tag_name,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                await self._db.execute(
                    "INSERT OR IGNORE INTO doc_tags (tag_id, doc_id) VALUES (?, ?)",
                    (row[0], meta.doc_id),
                )

        # Doc links (targets are already resolved to doc IDs by write/seed)
        await self._db.execute("DELETE FROM doc_links WHERE source_id = ?", (meta.doc_id,))
        for lnk in parsed.all_links:
            target = lnk.target
            if not target:
                continue
            # If target is already a doc_id, use it directly
            if _is_doc_id(target):
                target_id = target
            else:
                # Legacy path — resolve relative path to doc_id
                resolved_path = self._resolve_rel_path(meta.rel_path, target)
                target_meta = await self._get_doc_by_path(resolved_path)
                if target_meta is None:
                    stub_id = str(uuid.uuid4())
                    now_ms = _now_ms()
                    await self._db.execute(
                        "INSERT OR IGNORE INTO docs (doc_id, rel_path, title, created_at, modified_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (stub_id, resolved_path, None, now_ms, now_ms),
                    )
                    target_id = stub_id
                else:
                    target_id = target_meta.doc_id
            block_anchor = lnk.anchor or None
            await self._db.execute(
                "INSERT OR IGNORE INTO doc_links (source_id, target_id, block_anchor, display_text) "
                "VALUES (?, ?, ?, ?)",
                (meta.doc_id, target_id, block_anchor, lnk.display or None),
            )

        # Block index
        await self._db.execute("DELETE FROM doc_blocks WHERE doc_id = ?", (meta.doc_id,))
        for ord_idx, block in enumerate(parsed.blocks):
            if not block.block_id:
                continue
            snippet = block.text[:120].strip() if block.text else None
            await self._db.execute(
                "INSERT OR IGNORE INTO doc_blocks (doc_id, block_id, ord, kind, snippet) "
                "VALUES (?, ?, ?, ?, ?)",
                (meta.doc_id, block.block_id, ord_idx, block.kind, snippet),
            )

    # ── Link resolution (internal) ──────────────────────────────────────────

    def _resolve_rel_path(self, from_path: str, target: str) -> str:
        """Resolve a relative link target to a canonical vault-relative path.

        Rules:
        1. Ensure .bdx suffix
        2. Resolve relative to from_path's directory
        3. Normalize '../' and './' segments
        4. Return empty string if path escapes vault root
        """
        if not target:
            return ""
        if not target.endswith(_EXT):
            target = target + _EXT
        base_dir = Path(from_path).parent
        candidate = base_dir / target if not target.startswith("/") else Path(target.lstrip("/"))
        parts = candidate.parts
        normalized: list[str] = []
        for p in parts:
            if p == "..":
                if not normalized:
                    return ""  # Escapes root
                normalized.pop()
            elif p != ".":
                normalized.append(p)
        return "/".join(normalized)

    async def _resolve_links_to_doc_ids(
        self, xml_content: str, from_path: str
    ) -> tuple[str, bool]:
        """Scan XML for <bdx:link> elements and replace relative path targets with doc IDs.

        For each link with a non-doc-id target:
        - Resolve target relative to from_path
        - Look up the target doc's doc_id (creating a stub if needed)
        - Replace the target attribute with the doc_id

        Returns (modified_xml, changed).
        """
        assert self._db is not None
        changed = False

        async def _resolve_one(raw_target: str) -> str:
            nonlocal changed
            if not raw_target or _is_doc_id(raw_target):
                return raw_target
            resolved_path = self._resolve_rel_path(from_path, raw_target)
            if not resolved_path:
                return raw_target  # Invalid path, leave as-is
            target_meta = await self._get_doc_by_path(resolved_path)
            if target_meta is not None:
                changed = True
                return target_meta.doc_id
            # Create stub
            stub_id = str(uuid.uuid4())
            now_ms = _now_ms()
            await self._db.execute(
                "INSERT OR IGNORE INTO docs (doc_id, rel_path, title, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (stub_id, resolved_path, None, now_ms, now_ms),
            )
            changed = True
            return stub_id

        # Collect all matches first so we can resolve them asynchronously
        matches = list(_LINK_TARGET_RE.finditer(xml_content))
        if not matches:
            return xml_content, False

        # Resolve all targets
        replacements: list[tuple[int, int, str]] = []  # (start, end, new_target)
        for m in matches:
            raw_target = m.group(3)
            new_target = await _resolve_one(raw_target)
            if new_target != raw_target:
                # m.group(3) is the target value; we need to replace just that part
                target_start = m.start(3)
                target_end = m.end(3)
                replacements.append((target_start, target_end, new_target))

        if not replacements:
            return xml_content, False

        # Apply replacements in reverse order (to preserve indices)
        result = xml_content
        for start, end, new_target in sorted(replacements, reverse=True):
            result = result[:start] + new_target + result[end:]

        return result, True

    async def _remove_doc(self, doc_id: str, rel_path: str) -> None:
        assert self._db is not None
        await self._db.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))
        await self._db.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))

    async def _get_doc_by_path(self, rel_path: str) -> DocMeta | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT doc_id, rel_path, title, created_at, modified_at FROM docs WHERE rel_path = ?",
            (rel_path,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return DocMeta(
            doc_id=row[0], rel_path=row[1], title=row[2],
            created_at=row[3], modified_at=row[4],
        )

    # ── Path safety ───────────────────────────────────────────────────────

    def _safe_path(self, rel_path: str) -> Path:
        if not rel_path.endswith(_EXT):
            raise ValueError(f"Only {_EXT} files are supported: {rel_path!r}")
        p = (self._root / rel_path).resolve()
        if not str(p).startswith(str(self._root)):
            raise ValueError(f"Path traversal detected: {rel_path!r}")
        return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["DocMeta", "NoteVault"]
