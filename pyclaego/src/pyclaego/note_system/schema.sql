-- notes WidgetClass — SQLite schema v3
-- Applied by NoteVault._bootstrap_db() via executescript; all IF NOT EXISTS for idempotency.
-- Migrations run after CREATE statements (schema_version guard).

-- ── Document registry ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS docs (
    doc_id      TEXT    PRIMARY KEY,          -- UUID v4, stored in <bdx:meta>
    rel_path    TEXT    NOT NULL UNIQUE,      -- relative to doc_root, .bdx extension
    title       TEXT,                         -- first heading text; NULL until parsed
    created_at  INTEGER NOT NULL,             -- Unix ms
    modified_at INTEGER NOT NULL              -- Unix ms
);

CREATE INDEX IF NOT EXISTS idx_docs_rel_path    ON docs (rel_path);
CREATE INDEX IF NOT EXISTS idx_docs_modified_at ON docs (modified_at DESC);

-- ── Tag registry ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name TEXT    NOT NULL UNIQUE          -- lowercase, trimmed; pattern [a-z0-9_-]
);

-- ── Tag ↔ Doc membership ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_tags (
    tag_id  INTEGER NOT NULL REFERENCES tags(tag_id)  ON DELETE CASCADE,
    doc_id  TEXT    NOT NULL REFERENCES docs(doc_id)  ON DELETE CASCADE,
    PRIMARY KEY (tag_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_tags_doc_id ON doc_tags (doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_tags_tag_id ON doc_tags (tag_id);

-- ── Doc → Doc reference graph (directed: source cites target) ─────────────
-- block_anchor: the target block id (b_xxxx) this link points to, or NULL for doc-level.
-- PK includes block_anchor so the same source can reference multiple blocks in the same target.
CREATE TABLE IF NOT EXISTS doc_links (
    source_id    TEXT NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
    target_id    TEXT NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
    block_anchor TEXT,                                  -- target block id (b_xxxx) or NULL
    display_text TEXT,                                  -- display / alias text
    PRIMARY KEY (source_id, target_id, block_anchor)
);

CREATE INDEX IF NOT EXISTS idx_doc_links_source ON doc_links (source_id);
CREATE INDEX IF NOT EXISTS idx_doc_links_target ON doc_links (target_id);

-- ── Block registry (for anchor lookup and sidebar positioning) ────────────
-- Populated during _index_doc from every <bdx:block id="b_xxxx"> element.
CREATE TABLE IF NOT EXISTS doc_blocks (
    doc_id    TEXT    NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
    block_id  TEXT    NOT NULL,                         -- b_xxxx
    ord       INTEGER NOT NULL,                         -- 0-based index within document
    kind      TEXT    NOT NULL,                         -- paragraph/heading/code/…
    snippet   TEXT,                                     -- first ~120 chars of block text
    PRIMARY KEY (doc_id, block_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_blocks_doc ON doc_blocks (doc_id);

-- ── Full-text search (FTS5) ────────────────────────────────────────────────
-- body column holds plain-text extracted from XML (not raw XML).
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    doc_id   UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 1'
);

-- ── Schema version ────────────────────────────────────────────────────────
-- Bump PRAGMA user_version when making breaking schema changes.
-- Current: 3  (bdx format, block_anchor on doc_links, doc_blocks table)
PRAGMA user_version = 3;
