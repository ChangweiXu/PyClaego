-- notes WidgetClass — SQLite schema v2
-- Applied by NoteVault.bootstrap_db() via executescript; all IF NOT EXISTS for idempotency.

-- ── Document registry ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS docs (
    doc_id      TEXT    PRIMARY KEY,          -- UUID v4, injected in YAML frontmatter
    rel_path    TEXT    NOT NULL UNIQUE,      -- relative to doc_root, forward-slash separated
    title       TEXT,                         -- first H1/H2/... heading in body; NULL until parsed
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
CREATE TABLE IF NOT EXISTS doc_links (
    source_id    TEXT NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
    target_id    TEXT NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
    display_text TEXT,                                  -- optional anchor / display hint
    PRIMARY KEY (source_id, target_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_links_source ON doc_links (source_id);
CREATE INDEX IF NOT EXISTS idx_doc_links_target ON doc_links (target_id);

-- ── Full-text search (FTS5) ────────────────────────────────────────────────
-- Stores its own copy of title+body so we don't need external-content triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    doc_id   UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 1'
);
