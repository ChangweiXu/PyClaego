"""Tests for tiptap_json_converter.py — Tiptap JSON ↔ BDX XML round-trip."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyclaego.note_system.tiptap_json_converter import (
    tiptap_json_to_xml,
    xml_to_tiptap_json,
)

# ===========================================================================
# Round-trip: JSON → XML → JSON
# ===========================================================================


def _roundtrip(tiptap_json: dict) -> dict:
    """Convert JSON→XML→JSON and return the final JSON."""
    xml = tiptap_json_to_xml(tiptap_json)
    return xml_to_tiptap_json(xml)


def _json_eq(a: dict, b: dict) -> bool:
    """Deep equality ignoring key order."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class TestRoundTripBasic:
    """Basic documents: paragraphs, headings."""

    def test_empty_doc(self):
        doc = {"type": "doc", "content": [{"type": "paragraph"}]}
        result = _roundtrip(doc)
        assert result["type"] == "doc"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "paragraph"

    def test_single_paragraph(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "paragraph"
        assert result["content"][0]["content"][0]["text"] == "Hello world"

    def test_heading(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Section"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "heading"
        assert result["content"][0]["attrs"]["level"] == 2
        assert result["content"][0]["content"][0]["text"] == "Section"

    def test_multiple_blocks(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "First"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Second"}],
                },
            ],
        }
        result = _roundtrip(doc)
        assert len(result["content"]) == 2

    def test_code_block(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "codeBlock",
                    "attrs": {"language": "python"},
                    "content": [{"type": "text", "text": "print('hello')"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "codeBlock"
        assert result["content"][0]["attrs"]["language"] == "python"
        assert result["content"][0]["content"][0]["text"] == "print('hello')"


class TestRoundTripLists:
    """Bullet and ordered lists."""

    def test_bullet_list(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 1"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 2"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "bulletList"
        items = result["content"][0]["content"]
        assert len(items) == 2
        assert items[0]["content"][0]["content"][0]["text"] == "Item 1"

    def test_ordered_list(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "First"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "orderedList"


class TestRoundTripOtherBlocks:
    """Blockquote, image, horizontalRule."""

    def test_blockquote(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "blockquote",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Quoted"}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "blockquote"
        para = result["content"][0]["content"][0]
        assert para["content"][0]["text"] == "Quoted"

    def test_image(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "image",
                    "attrs": {"src": "http://example.com/img.png", "alt": "Example"},
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "image"
        assert result["content"][0]["attrs"]["src"] == "http://example.com/img.png"

    def test_horizontal_rule(self):
        doc = {
            "type": "doc",
            "content": [{"type": "horizontalRule"}],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "horizontalRule"


# ===========================================================================
# Marks round-trip
# ===========================================================================


class TestRoundTripMarks:
    """Single marks: bold, italic, underline, strike, code."""

    def test_bold(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "bold text", "marks": [{"type": "bold"}]}
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert text_node["text"] == "bold text"
        assert any(m["type"] == "bold" for m in text_node.get("marks", []))

    def test_italic(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "italic", "marks": [{"type": "italic"}]}
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "italic" for m in text_node.get("marks", []))

    def test_underline(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "under", "marks": [{"type": "underline"}]}
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "underline" for m in text_node.get("marks", []))

    def test_strike(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "deleted", "marks": [{"type": "strike"}]}
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "strike" for m in text_node.get("marks", []))

    def test_code_mark(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "inline_code", "marks": [{"type": "code"}]}
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "code" for m in text_node.get("marks", []))

    def test_highlight(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "highlighted", "marks": [{"type": "highlight"}]}
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "highlight" for m in text_node.get("marks", []))

    def test_highlight_with_color(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "yellow highlight",
                            "marks": [{"type": "highlight", "attrs": {"color": "#ffff00"}}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        hl_mark = next((m for m in marks if m["type"] == "highlight"), None)
        assert hl_mark is not None
        assert hl_mark.get("attrs", {}).get("color") == "#ffff00"


class TestRoundTripNestedMarks:
    """Multiple marks on same text (bold+italic, etc.)."""

    def test_bold_italic(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold+italic",
                            "marks": [{"type": "bold"}, {"type": "italic"}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        types = {m["type"] for m in marks}
        assert "bold" in types
        assert "italic" in types

    def test_bold_italic_underline(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "rich text",
                            "marks": [
                                {"type": "bold"},
                                {"type": "italic"},
                                {"type": "underline"},
                            ],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        types = {m["type"] for m in marks}
        assert types == {"bold", "italic", "underline"}


class TestRoundTripLinksAndTags:
    """Link and tag marks survive round-trip."""

    def test_link(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Click here",
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {"href": "550e8400-e29b-41d4-a716-446655440000#b_abc1"},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        link_mark = next((m for m in marks if m["type"] == "link"), None)
        assert link_mark is not None
        assert link_mark["attrs"]["href"] == "550e8400-e29b-41d4-a716-446655440000#b_abc1"

    def test_tag(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "#research",
                            "marks": [{"type": "tag", "attrs": {"name": "research"}}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        tag_mark = next((m for m in marks if m["type"] == "tag"), None)
        assert tag_mark is not None
        assert tag_mark["attrs"]["name"] == "research"

    def test_link_and_tag_and_bold(self):
        """Link + tag + bold on same text: link and bold combine on same node.
        Link wins (self-closing with display text), bold is suppressed to avoid
        text duplication — a limitation of BDX's self-closing link model."""
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "linked",
                            "marks": [
                                {"type": "link", "attrs": {"href": "uuid-123"}},
                                {"type": "bold"},
                            ],
                        },
                        {
                            "type": "text",
                            "text": "#tagged",
                            "marks": [{"type": "tag", "attrs": {"name": "tagged"}}],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        content = result["content"][0]["content"]
        # First node: link (bold suppressed since link is self-closing)
        # Second node: tag
        assert len(content) == 2
        link_node = content[0]
        assert any(m["type"] == "link" for m in link_node.get("marks", []))


# ===========================================================================
# XML → JSON (backward compat with existing .bdx files)
# ===========================================================================


_SAMPLE_BDX = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
      <bdx:meta>
        <bdx:title>Sample Note</bdx:title>
      </bdx:meta>
      <bdx:body>
        <bdx:block type="paragraph" id="b_p001">
          <bdx:content><![CDATA[Hello world.]]></bdx:content>
        </bdx:block>
      </bdx:body>
    </bdx:doc>
""")


class TestXmlToJson:
    """Parse existing .bdx XML to Tiptap JSON."""

    def test_parse_simple_xml(self):
        result = xml_to_tiptap_json(_SAMPLE_BDX)
        assert result["type"] == "doc"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "paragraph"
        assert result["content"][0]["content"][0]["text"] == "Hello world."

    def test_parse_empty_returns_paragraph(self):
        result = xml_to_tiptap_json("")
        assert result["type"] == "doc"
        assert result["content"][0]["type"] == "paragraph"

    def test_parse_xml_with_links(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" id="b_p001">
                  <bdx:content><![CDATA[See ]]></bdx:content>
                  <bdx:link target="other.bdx" anchor="b_x001" display="other note"/>
                  <bdx:content><![CDATA[.]]></bdx:content>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        content = result["content"][0]["content"]
        # Should have 3 nodes: "See ", link, "."
        assert len(content) == 3
        link_node = content[1]
        assert link_node["text"] == "other note"
        assert any(m["type"] == "link" for m in link_node.get("marks", []))

    def test_parse_xml_with_tags(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" id="b_p001">
                  <bdx:tag name="research"/>
                  <bdx:content><![CDATA[ important]]></bdx:content>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        content = result["content"][0]["content"]
        tag_node = content[0]
        assert tag_node["text"] == "#research"
        assert any(m["type"] == "tag" for m in tag_node.get("marks", []))

    def test_parse_xml_with_bold(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" id="b_p001">
                  <bdx:bold><bdx:content><![CDATA[bold text]]></bdx:content></bdx:bold>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        content = result["content"][0]["content"]
        assert content[0]["text"] == "bold text"
        assert any(m["type"] == "bold" for m in content[0].get("marks", []))

    def test_parse_xml_with_nested_bold_italic(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" id="b_p001">
                  <bdx:bold><bdx:italic><bdx:content><![CDATA[rich]]></bdx:content></bdx:italic></bdx:bold>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        content = result["content"][0]["content"]
        assert content[0]["text"] == "rich"
        types = {m["type"] for m in content[0].get("marks", [])}
        assert types == {"bold", "italic"}

    def test_parse_xml_with_code_block(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="code" lang="python" id="b_c001">
                  <bdx:content><![CDATA[print("hello")]]></bdx:content>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        assert result["content"][0]["type"] == "codeBlock"
        assert result["content"][0]["attrs"]["language"] == "python"

    def test_parse_xml_all_block_types(self):
        """Parse all supported block types from XML."""
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="heading" level="1" id="b_h001"><bdx:content><![CDATA[Title]]></bdx:content></bdx:block>
                <bdx:block type="paragraph" id="b_p001"><bdx:content><![CDATA[Text]]></bdx:content></bdx:block>
                <bdx:block type="code" lang="python" id="b_c001"><bdx:content><![CDATA[code]]></bdx:content></bdx:block>
                <bdx:block type="quote" id="b_q001"><bdx:content><![CDATA[Quote]]></bdx:content></bdx:block>
                <bdx:block type="list" style="unordered" id="b_l001">
                  <bdx:listItem id="b_l001a"><bdx:content><![CDATA[Item 1]]></bdx:content></bdx:listItem>
                  <bdx:listItem id="b_l001b"><bdx:content><![CDATA[Item 2]]></bdx:content></bdx:listItem>
                </bdx:block>
                <bdx:block type="list" style="ordered" id="b_l002">
                  <bdx:listItem id="b_l002a"><bdx:content><![CDATA[First]]></bdx:content></bdx:listItem>
                </bdx:block>
                <bdx:block type="image" src="img.png" alt="pic" id="b_i001"/>
                <bdx:block type="divider" id="b_d001"/>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        types = [b["type"] for b in result["content"]]
        assert types == [
            "heading", "paragraph", "codeBlock", "blockquote",
            "bulletList", "orderedList", "image", "horizontalRule",
        ]


# ===========================================================================
# Meta preservation
# ===========================================================================


class TestMetaPreservation:
    """When existing_xml is passed to tiptap_json_to_xml, meta is preserved."""

    def test_meta_preserved(self):
        existing = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:meta>
                <bdx:doc_id>test-doc-001</bdx:doc_id>
                <bdx:title>My Title</bdx:title>
              </bdx:meta>
              <bdx:body>
                <bdx:block type="paragraph"><bdx:content><![CDATA[old]]></bdx:content></bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        new_json = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "new"}]}
            ],
        }
        xml = tiptap_json_to_xml(new_json, existing_xml=existing)
        assert "test-doc-001" in xml
        assert "My Title" in xml
        assert "new" in xml
        assert "old" not in xml  # body replaced, meta preserved


# ===========================================================================
# textAlign — block-level alignment
# ===========================================================================


class TestRoundTripTextAlign:
    """Text alignment on paragraphs and headings."""

    def test_paragraph_align_center(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"textAlign": "center"},
                    "content": [{"type": "text", "text": "centered"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["attrs"]["textAlign"] == "center"

    def test_paragraph_align_right(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"textAlign": "right"},
                    "content": [{"type": "text", "text": "right-aligned"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["attrs"]["textAlign"] == "right"

    def test_paragraph_align_left(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"textAlign": "left"},
                    "content": [{"type": "text", "text": "left"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["attrs"]["textAlign"] == "left"

    def test_heading_align_center(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2, "textAlign": "center"},
                    "content": [{"type": "text", "text": "Centered Heading"}],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "heading"
        assert result["content"][0]["attrs"]["textAlign"] == "center"
        assert result["content"][0]["attrs"]["level"] == 2

    def test_parse_xml_with_align(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" align="right" id="b_p001">
                  <bdx:content><![CDATA[right-aligned]]></bdx:content>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        assert result["content"][0]["attrs"]["textAlign"] == "right"
        assert result["content"][0]["attrs"]["id"] == "b_p001"


# ===========================================================================
# Task list
# ===========================================================================


class TestRoundTripTaskList:
    """Task list with checked/unchecked items."""

    def test_tasklist_single_unchecked(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "taskList",
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"checked": False},
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Todo item"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "taskList"
        ti = result["content"][0]["content"][0]
        assert ti["type"] == "taskItem"
        assert ti["attrs"]["checked"] is False
        assert ti["content"][0]["content"][0]["text"] == "Todo item"

    def test_tasklist_mixed(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "taskList",
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"checked": True},
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Done"}],
                                }
                            ],
                        },
                        {
                            "type": "taskItem",
                            "attrs": {"checked": False},
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Not done"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        items = result["content"][0]["content"]
        assert items[0]["attrs"]["checked"] is True
        assert items[1]["attrs"]["checked"] is False

    def test_tasklist_with_bold_text(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "taskList",
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"checked": False},
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "important ", "marks": [{"type": "bold"}]},
                                        {"type": "text", "text": "task"},
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        ti = result["content"][0]["content"][0]
        content = ti["content"][0]["content"]
        assert any(m["type"] == "bold" for m in content[0].get("marks", []))

    def test_parse_xml_with_tasklist(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="taskList" id="b_t001">
                  <bdx:taskItem checked="true" id="b_ti1">
                    <bdx:content><![CDATA[done]]></bdx:content>
                  </bdx:taskItem>
                  <bdx:taskItem checked="false" id="b_ti2">
                    <bdx:content><![CDATA[todo]]></bdx:content>
                  </bdx:taskItem>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        assert result["content"][0]["type"] == "taskList"
        items = result["content"][0]["content"]
        assert items[0]["attrs"]["checked"] is True
        assert items[0]["content"][0]["content"][0]["text"] == "done"
        assert items[1]["attrs"]["checked"] is False


# ===========================================================================
# Table
# ===========================================================================


class TestRoundTripTable:
    """Table with headers and cells."""

    def test_table_simple_2x2(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "A1"}]}
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "B1"}]}
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "A2"}]}
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "B2"}]}
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        assert result["content"][0]["type"] == "table"
        rows = result["content"][0]["content"]
        assert len(rows) == 2
        assert rows[0]["content"][0]["content"][0]["content"][0]["text"] == "A1"
        assert rows[1]["content"][1]["content"][0]["content"][0]["text"] == "B2"

    def test_table_with_header(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "Col 1"}]}
                                    ],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "Col 2"}]}
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "Val 1"}]}
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "Val 2"}]}
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        rows = result["content"][0]["content"]
        assert rows[0]["content"][0]["type"] == "tableHeader"
        assert rows[0]["content"][0]["content"][0]["content"][0]["text"] == "Col 1"
        assert rows[1]["content"][0]["type"] == "tableCell"

    def test_table_empty_cell(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph"}
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "has text"}]}
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        rows = result["content"][0]["content"]
        # Empty cell should survive round-trip
        assert rows[0]["content"][0]["type"] == "tableCell"
        assert rows[0]["content"][1]["content"][0]["content"][0]["text"] == "has text"

    def test_parse_xml_with_table(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="table" id="b_t001">
                  <bdx:tableRow>
                    <bdx:tableHeader><bdx:content><![CDATA[H1]]></bdx:content></bdx:tableHeader>
                    <bdx:tableHeader><bdx:content><![CDATA[H2]]></bdx:content></bdx:tableHeader>
                  </bdx:tableRow>
                  <bdx:tableRow>
                    <bdx:tableCell><bdx:content><![CDATA[A1]]></bdx:content></bdx:tableCell>
                    <bdx:tableCell><bdx:content><![CDATA[B1]]></bdx:content></bdx:tableCell>
                  </bdx:tableRow>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        assert result["content"][0]["type"] == "table"
        rows = result["content"][0]["content"]
        assert rows[0]["content"][0]["type"] == "tableHeader"
        assert rows[0]["content"][0]["content"][0]["content"][0]["text"] == "H1"
        assert rows[1]["content"][1]["content"][0]["content"][0]["text"] == "B1"


# ===========================================================================
# Color / textStyle
# ===========================================================================


class TestRoundTripColor:
    """Color (textStyle) mark round-trip."""

    def test_color_mark(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "red text",
                            "marks": [{"type": "textStyle", "attrs": {"color": "#ff0000"}}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        color_mark = next((m for m in marks if m["type"] == "textStyle"), None)
        assert color_mark is not None
        assert color_mark.get("attrs", {}).get("color") == "#ff0000"

    def test_color_nested_with_bold(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold red",
                            "marks": [
                                {"type": "bold"},
                                {"type": "textStyle", "attrs": {"color": "#ff0000"}},
                            ],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        marks = text_node.get("marks", [])
        types = {m["type"] for m in marks}
        assert "bold" in types
        assert "textStyle" in types

    def test_parse_xml_with_color(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" id="b_p001">
                  <bdx:color value="#ff0000"><bdx:content><![CDATA[red]]></bdx:content></bdx:color>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        text_node = result["content"][0]["content"][0]
        assert text_node["text"] == "red"
        marks = text_node.get("marks", [])
        color_mark = next((m for m in marks if m["type"] == "textStyle"), None)
        assert color_mark is not None
        assert color_mark["attrs"]["color"] == "#ff0000"


# ===========================================================================
# Subscript / Superscript
# ===========================================================================


class TestRoundTripSubSuperScript:
    """Subscript and superscript marks round-trip."""

    def test_subscript(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "H2O",
                            "marks": [{"type": "subscript"}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "subscript" for m in text_node.get("marks", []))

    def test_superscript(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "E=mc",
                            "marks": [{"type": "superscript"}],
                        }
                    ],
                }
            ],
        }
        result = _roundtrip(doc)
        text_node = result["content"][0]["content"][0]
        assert any(m["type"] == "superscript" for m in text_node.get("marks", []))

    def test_parse_xml_with_subscript(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <bdx:doc xmlns:bdx="https://pyclaego.local/bdx/v1">
              <bdx:body>
                <bdx:block type="paragraph" id="b_p001">
                  <bdx:subscript><bdx:content><![CDATA[sub]]></bdx:content></bdx:subscript>
                </bdx:block>
              </bdx:body>
            </bdx:doc>
        """)
        result = xml_to_tiptap_json(xml)
        text_node = result["content"][0]["content"][0]
        assert text_node["text"] == "sub"
        assert any(m["type"] == "subscript" for m in text_node.get("marks", []))


# ===========================================================================
# Full multi-format document round-trip
# ===========================================================================


class TestRoundTripFullDocument:
    """A document with all new format types mixed together."""

    def test_full_document(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1, "textAlign": "center"},
                    "content": [{"type": "text", "text": "Full Test"}],
                },
                {
                    "type": "paragraph",
                    "attrs": {"textAlign": "right"},
                    "content": [
                        {"type": "text", "text": "right-aligned with "},
                        {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
                    ],
                },
                {
                    "type": "taskList",
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"checked": True},
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "done ", "marks": [{"type": "strike"}]},
                                        {"type": "text", "text": "task"},
                                    ],
                                }
                            ],
                        },
                    ],
                },
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Key"}]}],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Value"}]}],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "color"}]}],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": "red",
                                                    "marks": [{"type": "textStyle", "attrs": {"color": "#ff0000"}}],
                                                }
                                            ],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ],
        }
        result = _roundtrip(doc)

        # Verify heading with alignment
        h = result["content"][0]
        assert h["type"] == "heading"
        assert h["attrs"]["textAlign"] == "center"

        # Verify paragraph with alignment
        p = result["content"][1]
        assert p["attrs"]["textAlign"] == "right"
        assert any(m["type"] == "bold" for m in p["content"][1].get("marks", []))

        # Verify task list with checked + strike
        tl = result["content"][2]
        assert tl["type"] == "taskList"
        ti = tl["content"][0]
        assert ti["attrs"]["checked"] is True
        assert any(m["type"] == "strike" for m in ti["content"][0]["content"][0].get("marks", []))

        # Verify table with header and colored cell
        tbl = result["content"][3]
        assert tbl["type"] == "table"
        assert tbl["content"][0]["content"][0]["type"] == "tableHeader"
        val_cell = tbl["content"][1]["content"][1]
        val_marks = val_cell["content"][0]["content"][0].get("marks", [])
        color_m = next((m for m in val_marks if m["type"] == "textStyle"), None)
        assert color_m is not None
        assert color_m["attrs"]["color"] == "#ff0000"
