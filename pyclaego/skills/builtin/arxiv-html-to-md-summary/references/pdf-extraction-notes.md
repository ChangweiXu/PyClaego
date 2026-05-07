# PDF Extraction Notes for arXiv Papers

## When to use read_pdf

Always call `read_pdf` after downloading the arXiv PDF. This gives you:
- A structured outline (section headings → line numbers)
- The full text extracted as Markdown
- Page count for size assessment

## read_pdf call pattern

```python
result = read_pdf(path="paper-arxiv-2605.00658.pdf")
# Returns:
#   pdf_path:       absolute path to the PDF
#   file_size:      bytes
#   page_count:     total pages
#   md_available:   True if extraction succeeded
#   md_path:        path to cached Markdown (.cache/pdf_md/{hash}/content.md)
#   content_length: characters in extracted Markdown
#   outline:        list of {level, title, line} for each heading
#   preview:        first 500 chars of extracted text
```

## Targeted reading with outline

The outline maps headings to line numbers in the cached Markdown. Use this to read only relevant sections:

```python
# Example: read the Introduction section
outline = result.outline
intro_line = next(h.line for h in outline if h.title == "1 Introduction")
conclusion_line = next(h.line for h in outline if h.title == "5 Conclusion")
read_file(md_path, offset=intro_line, limit=conclusion_line - intro_line)
```

## Common issues and fixes

| Issue | Cause | Fix |
|-------|-------|-----|
| Garbled equations | PDF uses MathType/embedded fonts | Compare against HTML; HTML is more reliable for LaTeX math |
| Missing figures | Figures are raster, not vector | HTML version typically has readable captions at minimum |
| Single-column tables misaligned | PDF column layout | Read surrounding paragraph text instead |
| `page_count: 0` | Extraction failed (old bug) | Fixed in read_pdf_tool.py — now uses fallback PDF metadata reader |
| Content truncated | File > 8MB | Smart fallback: DocumentPart for small PDFs, empty + guidance for large |

## Cross-validation rules

1. **Metrics/numbers**: Prefer PDF. Tables are typically cleaner in the PDF extraction.
2. **Equations**: Prefer HTML. arXiv HTML uses MathJax/LaTeXML which preserves LaTeX.
3. **Author list**: Either source, both reliable.
4. **Figure descriptions**: HTML captions are often better formatted.
5. **When they disagree**: Default to PDF for tables, HTML for equations.

## Saving the PDF Markdown

After extraction, copy the cached Markdown to the working directory:

```python
copy_move(source=result.md_path, destination="paper-arxiv-2605.00658-pdf_md.md", action="copy")
```

This ensures persistence beyond the cache TTL.

## Large paper strategy

For papers > 30 pages (e.g., surveys, theses):
1. Read outline only
2. Read Introduction (lines from outline)
3. Read Method/Approach overview
4. Read Conclusion
5. Skip detailed experiments unless user asks
