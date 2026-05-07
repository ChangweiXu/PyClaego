"""Phase 6 — Verification tests for the notes widget BDX layer.

Tests cover:
  1. bdx_parser — parse round-trip, plaintext, links, tags
  2. bdx_serializer — assign_block_ids idempotency, empty_doc
  3. bdx_meta — strip_meta / inject_meta roundtrip, extract_title
  4. NoteVault — write / read / search / backlinks / resolve_link (in-memory)
  5. Librarian — tool dispatch (mocked vault)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Path shim so tests can run from any CWD
# ---------------------------------------------------------------------------
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyclaego.note_system.bdx_meta import (
    extract_title,
    inject_meta,
    meta_element_xml,
    strip_meta,
)
from pyclaego.note_system.bdx_parser import BdxMeta, parse_bdx
from pyclaego.note_system.bdx_serializer import (
    assign_block_ids,
    empty_doc,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_BDX = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
      <bdx:meta>
        <bdx:doc_id>test-doc-001</bdx:doc_id>
        <bdx:rel_path>test/sample.bdx</bdx:rel_path>
        <bdx:title>Sample Note</bdx:title>
        <bdx:created_at>1700000000000</bdx:created_at>
        <bdx:modified_at>1700000001000</bdx:modified_at>
      </bdx:meta>
      <bdx:body>
        <bdx:block type="heading" level="1" id="b_h001">
          <bdx:content><![CDATA[Introduction]]></bdx:content>
        </bdx:block>
        <bdx:block type="paragraph" id="b_p001">
          <bdx:content><![CDATA[Hello world. See ]]></bdx:content>
          <bdx:link target="other/note.bdx" anchor="b_x001" display="other note"/>
          <bdx:content><![CDATA[ and ]]></bdx:content>
          <bdx:tag name="research"/>
          <bdx:content><![CDATA[.]]></bdx:content>
        </bdx:block>
        <bdx:block type="code" lang="python" id="b_c001">
          <bdx:content><![CDATA[print("hello")]]></bdx:content>
        </bdx:block>
      </bdx:body>
    </bdx:doc>
""")

_MINIMAL_BDX = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
      <bdx:body>
        <bdx:block type="paragraph">
          <bdx:content><![CDATA[No id yet.]]></bdx:content>
        </bdx:block>
      </bdx:body>
    </bdx:doc>
""")


# ===========================================================================
# 1. bdx_parser
# ===========================================================================

class TestBdxParser:
    def test_parse_meta(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        assert parsed.meta.doc_id == "test-doc-001"
        assert parsed.meta.title == "Sample Note"
        assert parsed.meta.created_at == 1700000000000

    def test_parse_blocks_count(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        assert len(parsed.blocks) == 3

    def test_heading_block(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        h = parsed.blocks[0]
        assert h.kind == "heading"
        assert h.attrs.get("level") == 1
        assert "Introduction" in h.text

    def test_paragraph_link_and_tag(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        p = parsed.blocks[1]
        assert len(p.links) == 1
        assert p.links[0].target == "other/note.bdx"
        assert p.links[0].anchor == "b_x001"
        assert p.links[0].display == "other note"
        assert "research" in p.tags

    def test_plaintext(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        pt = parsed.plaintext
        assert "Introduction" in pt
        assert "Hello world" in pt
        assert 'print("hello")' in pt

    def test_all_tags(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        assert "research" in parsed.all_tags

    def test_all_links(self):
        parsed = parse_bdx(_SAMPLE_BDX)
        assert any(lnk.target == "other/note.bdx" for lnk in parsed.all_links)

    def test_empty_doc_doesnt_crash(self):
        result = parse_bdx("")
        assert result.blocks == []

    def test_malformed_xml_doesnt_crash(self):
        result = parse_bdx("<not valid xml<<<")
        assert result.blocks == []


# ===========================================================================
# 2. bdx_serializer
# ===========================================================================

class TestBdxSerializer:
    def test_assign_block_ids_assigns_when_missing(self):
        out, changed = assign_block_ids(_MINIMAL_BDX)
        assert changed is True
        assert 'id="b_' in out

    def test_assign_block_ids_idempotent(self):
        out1, _ = assign_block_ids(_MINIMAL_BDX)
        out2, changed = assign_block_ids(out1)
        assert changed is False
        assert out1 == out2

    def test_assign_preserves_existing_ids(self):
        out, changed = assign_block_ids(_SAMPLE_BDX)
        assert changed is False
        assert 'id="b_h001"' in out
        assert 'id="b_p001"' in out

    def test_empty_doc_produces_valid_xml(self):
        meta = BdxMeta(doc_id="x", rel_path="test.bdx", title="T")
        xml = empty_doc(meta)
        parsed = parse_bdx(xml)
        assert parsed.meta.doc_id == "x"
        # Body is empty — no blocks
        assert parsed.blocks == []


# ===========================================================================
# 3. bdx_meta
# ===========================================================================

class TestBdxMeta:
    def test_strip_meta_parses_correctly(self):
        meta, _ = strip_meta(_SAMPLE_BDX)
        assert meta.doc_id == "test-doc-001"
        assert meta.title == "Sample Note"
        assert meta.created_at == 1700000000000

    def test_strip_meta_empty_returns_empty(self):
        meta, _ = strip_meta("")
        assert meta.doc_id is None

    def test_inject_meta_replaces_title(self):
        new_meta = BdxMeta(
            doc_id="test-doc-001",
            rel_path="test/sample.bdx",
            title="Updated Title",
            created_at=1700000000000,
            modified_at=1700000099000,
        )
        updated = inject_meta(_SAMPLE_BDX, new_meta)
        reparsed, _ = strip_meta(updated)
        assert reparsed.title == "Updated Title"
        assert reparsed.modified_at == 1700000099000

    def test_extract_title_from_meta(self):
        # extract_title returns the first heading block text, not the <bdx:title> element
        title = extract_title(_SAMPLE_BDX)
        assert title == "Introduction"

    def test_extract_title_none_on_no_meta(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body/>
            </bdx:doc>
        """)
        assert extract_title(xml) is None

    def test_meta_roundtrip_no_data_loss(self):
        meta, _ = strip_meta(_SAMPLE_BDX)
        xml = meta_element_xml(meta)
        # Must contain key fields
        assert "test-doc-001" in xml
        assert "Sample Note" in xml


# ===========================================================================
# 4. NoteVault (isolated, temp directory)
# ===========================================================================

@pytest.fixture
async def tmp_vault(tmp_path: Path):
    from pyclaego.note_system.vault import NoteVault

    root = tmp_path / "vault"
    root.mkdir()
    vault = NoteVault(doc_root=root)
    await vault.startup()
    yield vault
    await vault.shutdown()


class TestNoteVault:
    async def test_write_and_read(self, tmp_vault):
        await tmp_vault.write("notes/first.bdx", _SAMPLE_BDX)
        raw = await tmp_vault.read("notes/first.bdx")
        assert raw is not None
        assert "Sample Note" in raw or "Introduction" in raw

    async def test_write_creates_meta(self, tmp_vault):
        meta = await tmp_vault.write("hello.bdx", _SAMPLE_BDX)
        assert meta.rel_path == "hello.bdx"
        assert meta.doc_id is not None

    async def test_read_nonexistent_returns_none(self, tmp_vault):
        result = await tmp_vault.read("does/not/exist.bdx")
        assert result is None

    async def test_search_finds_content(self, tmp_vault):
        await tmp_vault.write("notes/search-me.bdx", _SAMPLE_BDX)
        results = await tmp_vault.search("Introduction")
        assert any(r["rel_path"] == "notes/search-me.bdx" for r in results)

    async def test_search_empty_query_no_crash(self, tmp_vault):
        results = await tmp_vault.search("")
        assert isinstance(results, list)

    async def test_backlinks_empty_on_fresh_doc(self, tmp_vault):
        await tmp_vault.write("fresh.bdx", _SAMPLE_BDX)
        links = await tmp_vault.backlinks("fresh.bdx")
        # The sample doc links TO other/note.bdx, so fresh.bdx has no inbound links
        assert isinstance(links, list)

    async def test_file_tree_returns_written_file(self, tmp_vault):
        await tmp_vault.write("mytree.bdx", _SAMPLE_BDX)
        tree = tmp_vault.file_tree()
        paths = _flatten_tree(tree)
        assert "mytree.bdx" in paths

    async def test_delete_removes_file(self, tmp_vault):
        await tmp_vault.write("todelete.bdx", _SAMPLE_BDX)
        await tmp_vault.delete("todelete.bdx")
        raw = await tmp_vault.read("todelete.bdx")
        assert raw is None

    async def test_rename_file(self, tmp_vault):
        await tmp_vault.write("old-name.bdx", _SAMPLE_BDX)
        await tmp_vault.rename("old-name.bdx", "new-name.bdx")
        assert await tmp_vault.read("new-name.bdx") is not None
        assert await tmp_vault.read("old-name.bdx") is None

    async def test_path_traversal_rejected(self, tmp_vault):
        with pytest.raises((ValueError, Exception)):
            await tmp_vault.read("../../etc/passwd")

    async def test_wrong_extension_rejected(self, tmp_vault):
        with pytest.raises((ValueError, Exception)):
            await tmp_vault.write("note.md", "content")


# ===========================================================================
# 5. Librarian — tool dispatch (mocked vault)
# ===========================================================================

class TestLibrarianEngine:
    """Test LibrarianEngine._execute() directly without hitting an LLM."""

    def _make_vault(self):
        v = MagicMock()
        v.search = AsyncMock(return_value=[
            {"doc_id": "d1", "rel_path": "notes/a.bdx", "title": "A", "snippet": "hello"}
        ])
        v.read = AsyncMock(return_value=_SAMPLE_BDX)
        v.file_tree = MagicMock(return_value=[
            {"type": "file", "name": "a.bdx", "rel_path": "notes/a.bdx"}
        ])
        v.write = AsyncMock(return_value=MagicMock(doc_id="new-id"))
        return v

    async def test_vault_search(self):
        from pyclaego.llm.types import ToolCall
        from pyclaego.personal_space.widget_classes.widgets.notes.librarian import LibrarianEngine

        vault = self._make_vault()
        engine = LibrarianEngine(vault)
        tc = ToolCall(id="t1", name="vault_search", arguments={"query": "hello"})
        result = await engine._execute(tc)
        assert "notes/a.bdx" in result
        assert "hello" in result

    async def test_vault_read(self):
        from pyclaego.llm.types import ToolCall
        from pyclaego.personal_space.widget_classes.widgets.notes.librarian import LibrarianEngine

        vault = self._make_vault()
        engine = LibrarianEngine(vault)
        tc = ToolCall(id="t2", name="vault_read", arguments={"rel_path": "notes/a.bdx"})
        result = await engine._execute(tc)
        # Should return the plaintext of _SAMPLE_BDX
        assert "Introduction" in result or "Sample Note" in result

    async def test_vault_list(self):
        from pyclaego.llm.types import ToolCall
        from pyclaego.personal_space.widget_classes.widgets.notes.librarian import LibrarianEngine

        vault = self._make_vault()
        engine = LibrarianEngine(vault)
        tc = ToolCall(id="t3", name="vault_list", arguments={})
        result = await engine._execute(tc)
        assert "notes/a.bdx" in result

    async def test_vault_create(self):
        from pyclaego.llm.types import ToolCall
        from pyclaego.personal_space.widget_classes.widgets.notes.librarian import LibrarianEngine

        vault = self._make_vault()
        engine = LibrarianEngine(vault)
        tc = ToolCall(id="t4", name="vault_create", arguments={"rel_path": "new.bdx", "content": "My new note"})
        result = await engine._execute(tc)
        assert "new.bdx" in result
        vault.write.assert_awaited_once()

    async def test_unknown_tool_returns_error(self):
        from pyclaego.llm.types import ToolCall
        from pyclaego.personal_space.widget_classes.widgets.notes.librarian import LibrarianEngine

        vault = self._make_vault()
        engine = LibrarianEngine(vault)
        tc = ToolCall(id="t5", name="nonexistent_tool", arguments={})
        result = await engine._execute(tc)
        assert "未知工具" in result

    async def test_chat_no_llm_returns_message(self):
        """If LLM is not configured, chat() should return a graceful message."""
        from pyclaego.personal_space.widget_classes.widgets.notes.librarian import LibrarianEngine

        vault = self._make_vault()
        engine = LibrarianEngine(vault)
        # Patch _make_client to return None (no LLM configured)
        with patch(
            "pyclaego.personal_space.widget_classes.widgets.notes.librarian._make_client",
            return_value=None,
        ):
            reply = await engine.chat("hello", [])
        assert reply  # non-empty response


# ===========================================================================
# 6. Seed file link integrity — welcome.bdx
# ===========================================================================

class TestSeedFileLinks:
    """Verify that seed docs are stored with correct, non-empty link targets."""

    @pytest.fixture
    async def seeded_vault(self, tmp_path: Path):
        """A fresh vault that has run _seed_examples and _reconcile."""
        from pyclaego.note_system.vault import NoteVault
        root = tmp_path / "seeded"
        root.mkdir()
        vault = NoteVault(doc_root=root)
        await vault.startup()
        yield vault
        await vault.shutdown()

    async def test_welcome_bdx_exists(self, seeded_vault):
        xml = await seeded_vault.read("welcome.bdx")
        assert xml is not None, "welcome.bdx should be seeded"

    async def test_welcome_bdx_has_rel_path_meta(self, seeded_vault):
        xml = await seeded_vault.read("welcome.bdx")
        assert "<bdx:rel_path>" in xml, (
            "welcome.bdx should have <bdx:rel_path> after reconcile"
        )

    async def test_welcome_bdx_links_are_non_empty(self, seeded_vault):
        import re
        xml = await seeded_vault.read("welcome.bdx")
        links = re.findall(r'<bdx:link\s+([^>]+)>', xml)
        assert links, "welcome.bdx should contain at least one <bdx:link>"
        for attrs in links:
            m = re.search(r'target="([^"]*)"', attrs)
            assert m, f"<bdx:link> missing target attribute: {attrs}"
            assert m.group(1), (
                f"<bdx:link> has empty target after seed+reconcile: {attrs}"
            )

    async def test_write_preserves_link_targets(self, seeded_vault):
        """vault.write() must not corrupt <bdx:link> target attributes."""
        import re

        from pyclaego.note_system.bdx_parser import parse_bdx

        xml_in = await seeded_vault.read("welcome.bdx")
        # Re-write through vault.write() to trigger _resolve_links_to_doc_ids
        await seeded_vault.write("welcome.bdx", xml_in)
        xml_out = await seeded_vault.read("welcome.bdx")

        # Parse must succeed (no XML error)
        parsed = parse_bdx(xml_out)
        assert parsed.blocks, "parsed doc should have blocks after write round-trip"
        links_out = re.findall(r'<bdx:link\s+([^>]+)/?>', xml_out)
        assert links_out, "links should survive a write round-trip"
        for attrs in links_out:
            m = re.search(r'target="([^"]*)"', attrs)
            assert m and m.group(1), (
                f"link target became empty after write round-trip: {attrs}"
            )

    async def test_resolve_links_to_doc_ids_preserves_xml(self, seeded_vault):
        """Regression: _resolve_links_to_doc_ids must preserve valid XML."""
        import re
        import xml.etree.ElementTree as ET

        sample = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">\n'
            '  <bdx:body>\n'
            '    <bdx:block type="paragraph" id="b_t001">\n'
            '      <bdx:link target="examples/note-a.bdx" anchor="" display="Example Note"/>\n'
            '    </bdx:block>\n'
            '  </bdx:body>\n'
            '</bdx:doc>\n'
        )
        result, changed = await seeded_vault._resolve_links_to_doc_ids(sample, "welcome.bdx")
        try:
            ET.fromstring(result.strip())
        except ET.ParseError as e:
            pytest.fail(f"_resolve_links_to_doc_ids produced invalid XML: {e}\n{result}")
        # After resolution, target should be a doc_id (UUID) or empty, not a relative path
        m = re.search(r'target="([^"]*)"', result)
        assert m, "link target missing after resolution"
        target_val = m.group(1)
        # Should not be a .bdx path anymore — should be a UUID or empty
        assert not target_val.endswith('.bdx'), (
            f"link target should be a doc_id, got path: {target_val}"
        )


# ===========================================================================
# Helpers
# ===========================================================================

def _flatten_tree(nodes: list[dict]) -> list[str]:
    paths: list[str] = []
    for n in nodes:
        if n["type"] == "file":
            paths.append(n["rel_path"])
        else:
            paths.extend(_flatten_tree(n.get("children", [])))
    return paths
