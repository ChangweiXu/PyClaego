# Note B — Backlink Example

[[#example]] [[#research]]

This note is linked to by **Note A** (`[[examples/note-a.md:introduction]]`).

## What Are Backlinks?

A backlink is any note that links *to* this note. The `doc_links` table stores
directed edges, so backlinks are just the set of rows where `target_id = this_doc_id`.

The **Graph** drawer shows both outgoing and incoming links visually.

## Tag Example

This note uses `[[#research]]` to indicate it contains research content.
Open the **Tags** drawer, click `research`, and you'll see this note listed alongside
any other notes tagged with `[[#research]]`.

## Back-Navigation

[[examples/note-a.md]] — the note that links here
[[getting-started.md]] — the onboarding guide
