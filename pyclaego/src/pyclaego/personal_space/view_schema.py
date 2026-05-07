"""ViewSchema — discriminated union describing how a Widget's detail view should render.

Design rules:
- Content nodes reference data *by widget_id*, never by inlining payloads.
  The frontend cache (TanStack Query) owns the truth; the schema owns the layout intent.
- No style/color/padding props — appearance belongs to primitives, not the schema.
- Add `custom` as an escape hatch for genuinely bespoke React renderers.

Generate TypeScript types from this file:
    pip install datamodel-code-generator
    datamodel-codegen --input src/personal_space/view_schema.py \
        --input-file-type python \
        --output dashboard/src/schema/generated.ts \
        --output-model-type typescript
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Leaf / content nodes
# ---------------------------------------------------------------------------


class KVTableSchema(BaseModel):
    """Key-value table rendered as a two-column grid."""
    type: Literal["kv_table"] = "kv_table"
    rows: list[list[str]] = Field(
        default_factory=list,
        description="List of [key, value] string pairs.",
    )


class StatSchema(BaseModel):
    """Single metric display: label + big value + optional trend."""
    type: Literal["stat"] = "stat"
    label: str
    value: str | int | float
    trend: float | None = None   # positive = up, negative = down


class MarkdownSchema(BaseModel):
    """Inline markdown text rendered with remark-gfm."""
    type: Literal["markdown"] = "markdown"
    text: str


class ChatLogSchema(BaseModel):
    """Chat message log.  Messages live in the frontend cache keyed by widget_id."""
    type: Literal["chat_log"] = "chat_log"
    widget_id: str


class TaskListSchema(BaseModel):
    """Task status list.  Data lives in cache; schema just declares the widget source."""
    type: Literal["task_list"] = "task_list"
    widget_id: str
    filter: dict[str, Any] | None = None


class TreeNode(BaseModel):
    id: str
    label: str
    icon: str | None = None
    children: list[TreeNode] | None = None
    payload: dict[str, Any] | None = None   # arbitrary data forwarded on select


TreeNode.model_rebuild()


class TreeSchema(BaseModel):
    """Generic tree (file tree, task tree, …)."""
    type: Literal["tree"] = "tree"
    nodes: list[TreeNode] = Field(default_factory=list)
    on_select_command: str | None = None    # command name sent on node click


class DocumentListSchema(BaseModel):
    """Virtualized list of markdown documents identified by doc_ids."""
    type: Literal["document_list"] = "document_list"
    doc_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Layout nodes
# ---------------------------------------------------------------------------


class ButtonSpec(BaseModel):
    label: str
    command: str
    args: dict[str, Any] | None = None
    variant: Literal["primary", "danger", "ghost"] | None = None
    disabled: bool | None = None


class ToolbarSchema(BaseModel):
    """Row of command buttons."""
    type: Literal["toolbar"] = "toolbar"
    buttons: list[ButtonSpec] = Field(default_factory=list)


# Forward declarations resolved below
class SplitSchema(BaseModel):
    type: Literal["split"] = "split"
    orientation: Literal["h", "v"] = "h"   # h = left|right, v = top|bottom
    left: ViewSchema
    right: ViewSchema
    ratio: float = 0.6                      # fraction of space given to `left`


class TabItem(BaseModel):
    label: str
    content: ViewSchema


class TabsSchema(BaseModel):
    type: Literal["tabs"] = "tabs"
    tabs: list[TabItem] = Field(default_factory=list)


class StackSchema(BaseModel):
    type: Literal["stack"] = "stack"
    gap: int | None = None
    children: list[ViewSchema] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------


class CustomSchema(BaseModel):
    """Falls through to a named custom renderer registered on the frontend."""
    type: Literal["custom"] = "custom"
    renderer: str                            # key in customRenderers registry
    props: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Union
# ---------------------------------------------------------------------------

ViewSchema = Annotated[
    Union[
        # Layout
        SplitSchema,
        TabsSchema,
        StackSchema,
        ToolbarSchema,
        # Content
        ChatLogSchema,
        TaskListSchema,
        TreeSchema,
        MarkdownSchema,
        DocumentListSchema,
        KVTableSchema,
        StatSchema,
        # Escape hatch
        CustomSchema,
    ],
    Field(discriminator="type"),
]

# Rebuild models with forward refs
SplitSchema.model_rebuild()
TabItem.model_rebuild()
TabsSchema.model_rebuild()
StackSchema.model_rebuild()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class WidgetCommand(BaseModel):
    """Payload for POST /widgets/{id}/commands."""
    command: str
    args: dict[str, Any] = Field(default_factory=dict)


class CommandResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


__all__ = [
    "ButtonSpec",
    "ChatLogSchema",
    "CommandResult",
    "CustomSchema",
    "DocumentListSchema",
    "KVTableSchema",
    "MarkdownSchema",
    "SplitSchema",
    "StackSchema",
    "StatSchema",
    "TabItem",
    "TabsSchema",
    "TaskListSchema",
    "ToolbarSchema",
    "TreeNode",
    "TreeSchema",
    "ViewSchema",
    "WidgetCommand",
]
