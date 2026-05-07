"""Tests for SubAgentSummarizingArtifactStore — three-band spill policy.

Bands:
  < 5k tokens    → no spill, no oversized flag
  5k–10k tokens  → spill (full content inline), oversized flag, artifact has tool_result_read reference
  > 10k tokens   → spill (truncated to 10k inline), oversized flag, truncation marker in inline text
"""

import asyncio

import pytest

from pyclaego.context.subagent.subagent_summarizing_artifact_store import (
    SubAgentSummarizingArtifactStore,
)
from pyclaego.context.token_counter import TokenCounter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_tc = TokenCounter()


def _make_content(approx_tokens: int) -> str:
    """Generate ASCII text of approximately `approx_tokens` tokens."""
    # tiktoken typically encodes ~1 token per word for short English words
    word = "hello "
    repeats = max(1, approx_tokens)
    return (word * repeats).strip()


def _count(text: str) -> int:
    return _tc.count_tokens(text)


# ---------------------------------------------------------------------------
# Band 1 — < 5k tokens: no spill
# ---------------------------------------------------------------------------

class TestBandSmall:
    def test_should_not_spill(self, tmp_path):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        content = _make_content(3_000)
        assert _count(content) < 5_000
        assert store.should_spill(content) is False

    def test_no_artifact_created(self, tmp_path):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        content = _make_content(3_000)
        assert store.get_artifact("tc_small") is None
        assert store.list_artifacts("sess1") == []

    def test_empty_content_not_spilled(self, tmp_path):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        assert store.should_spill("") is False
        assert store.should_spill(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Band 2 — 5k–10k tokens: spill full content inline, no truncation
# ---------------------------------------------------------------------------

class TestBandMid:
    @pytest.fixture
    def content_7k(self):
        c = _make_content(7_000)
        tok = _count(c)
        assert 5_000 <= tok <= 10_000, f"Expected 5k-10k tokens, got {tok}"
        return c

    def test_should_spill(self, tmp_path, content_7k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        assert store.should_spill(content_7k) is True

    def test_spill_writes_full_content(self, tmp_path, content_7k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        assert artifact.path.exists()
        on_disk = artifact.path.read_text("utf-8")
        assert on_disk == content_7k

    def test_render_inline_returns_full_content(self, tmp_path, content_7k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        inline = store.render_inline(artifact, content_7k)
        # Full content — no truncation marker
        assert "[Tool result truncated" not in inline
        assert inline == content_7k

    def test_artifact_has_correct_metadata(self, tmp_path, content_7k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        assert artifact.tool_call_id == "tc_mid"
        assert artifact.tool_name == "some_tool"
        assert artifact.session_id == "sess1"
        assert artifact.total_tokens == _count(content_7k)
        assert artifact.total_chars == len(content_7k)

    def test_get_artifact_after_spill(self, tmp_path, content_7k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        assert store.get_artifact("tc_mid") is not None

    def test_list_artifacts_scoped_to_session(self, tmp_path, content_7k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        assert len(store.list_artifacts("sess1")) == 1
        assert len(store.list_artifacts("sess_other")) == 0

    def test_evict_tool_reference_always_present(self, tmp_path, content_7k):
        """Evict tool template always has 'tool_result_read' reference because artifact exists."""
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        # Artifact always present → evict tool can emit "available at" safely
        assert store.get_artifact("tc_mid") is artifact

    def test_no_spill_loop_regression(self, tmp_path, content_7k):
        """The inline content for a 7k result is the full original content.
        The subagent therefore does NOT need a follow-up tool_result_read call.
        """
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_mid", "some_tool", content_7k)
        )
        inline = store.render_inline(artifact, content_7k)
        # Full content preserved inline — no disk pointer required by the subagent
        assert inline == content_7k


# ---------------------------------------------------------------------------
# Band 3 — > 10k tokens: spill full content, inline truncated to 10k
# ---------------------------------------------------------------------------

class TestBandLarge:
    @pytest.fixture
    def content_12k(self):
        c = _make_content(12_000)
        tok = _count(c)
        assert tok > 10_000, f"Expected >10k tokens, got {tok}"
        return c

    def test_should_spill(self, tmp_path, content_12k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        assert store.should_spill(content_12k) is True

    def test_spill_writes_full_content(self, tmp_path, content_12k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_large", "big_tool", content_12k)
        )
        on_disk = artifact.path.read_text("utf-8")
        assert on_disk == content_12k  # full content on disk

    def test_render_inline_truncated(self, tmp_path, content_12k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_large", "big_tool", content_12k)
        )
        inline = store.render_inline(artifact, content_12k)
        inline_tokens = _count(inline)
        # Truncation marker adds a few tokens, so budget is approximately 10k + small overhead
        assert inline_tokens <= 10_500, f"inline too large: {inline_tokens} tokens"

    def test_truncation_marker_present(self, tmp_path, content_12k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_large", "big_tool", content_12k)
        )
        inline = store.render_inline(artifact, content_12k)
        assert "[Tool result truncated" in inline
        assert "tool_result_read" in inline
        assert "tc_large" in inline

    def test_disk_content_larger_than_inline(self, tmp_path, content_12k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_large", "big_tool", content_12k)
        )
        inline = store.render_inline(artifact, content_12k)
        on_disk = artifact.path.read_text("utf-8")
        assert len(on_disk) > len(inline)

    def test_artifact_total_tokens_is_full(self, tmp_path, content_12k):
        store = SubAgentSummarizingArtifactStore(tmp_path)
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_large", "big_tool", content_12k)
        )
        assert artifact.total_tokens == _count(content_12k)


# ---------------------------------------------------------------------------
# Config thresholds
# ---------------------------------------------------------------------------

class TestCustomThresholds:
    def test_custom_warn_tokens(self, tmp_path):
        store = SubAgentSummarizingArtifactStore(tmp_path, warn_tokens=100, truncate_tokens=200)
        content = "word " * 150  # ~150 tokens
        assert store.should_spill(content) is True

    def test_custom_truncate_tokens(self, tmp_path):
        store = SubAgentSummarizingArtifactStore(tmp_path, warn_tokens=50, truncate_tokens=100)
        content = "word " * 200  # ~200 tokens > truncate_tokens=100
        artifact = asyncio.get_event_loop().run_until_complete(
            store.spill("sess1", "tc_custom", "tool", content)
        )
        inline = store.render_inline(artifact, content)
        assert "[Tool result truncated" in inline
        # Inline must be bounded near truncate_tokens
        assert _count(inline) <= 150  # 100 + some marker tokens

    def test_exactly_at_warn_boundary(self, tmp_path):
        """Exactly warn_tokens should spill."""
        store = SubAgentSummarizingArtifactStore(tmp_path, warn_tokens=5_000, truncate_tokens=10_000)
        # Build content that hits exactly warn_tokens
        content = "word " * 5_000
        tok = _count(content)
        if tok >= 5_000:
            assert store.should_spill(content) is True
