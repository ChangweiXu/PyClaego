# Note A — Graph Linking Example

[[#example]] [[#graph]]

This note demonstrates bidirectional linking.

## Outgoing Links

This note links to **Note B**:

```
[[examples/note-b.md]]
```

Rendered: [[examples/note-b.md]]

And also back to the welcome page: [[welcome.md]]

## How the Graph Works

When you save a note, the backend parses every `[[path]]` occurrence and records
a directed edge `(this_doc → target_doc)` in the `doc_links` table.

The **Graph** drawer fetches these edges and renders them with Cytoscape.js.
Clicking any node opens that note in the editor.

## Stub Nodes

If you link to a file that doesn't exist yet:

```
[[future/planned-note.md]]
```

A **stub** entry is created in the database (visible as a dashed node in the graph).
Once you create that file, the stub is promoted to a real node — its `doc_id` is preserved.
