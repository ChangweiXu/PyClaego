---
name: arxiv-html-to-md-summary
description: Given an arXiv ID like 2604.15804, fetch the arXiv HTML page and PDF, save raw HTML/text/info/PDF files with the prefix paper-arxiv-<id>-<doc type>.*, convert the HTML body into Markdown via a bundled Python script, optionally extract PDF content via read_pdf for cross-validation, then produce a structured paper summary covering problem/pain points, proposed method/advantages, and experiments/comparisons/conclusions. Use when the user asks to read, parse, summarize, or export an arXiv paper from its arXiv ID or arXiv HTML page.
---

# arXiv HTML to Markdown + PDF + Summary

Use this skill when the user provides an arXiv ID and wants a reproducible workflow that:
1. resolves the arXiv HTML page and PDF,
2. downloads and stores source artifacts (HTML, text, PDF),
3. converts HTML to Markdown with Python,
4. optionally extracts PDF content via `read_pdf` for cross-validation,
5. summarizes the paper in a fixed structure.

## Required file naming

All generated files must use this prefix:
- `paper-arxiv-<id>-<doc type>.<ext>`

Examples for id `2604.15804`:
- `paper-arxiv-2604.15804-raw_html.html`
- `paper-arxiv-2604.15804-raw_text.txt`
- `paper-arxiv-2604.15804-info.json`
- `paper-arxiv-2604.15804-md.md`
- `paper-arxiv-2604.15804-summary.md`
- `paper-arxiv-2604.15804.pdf` — the downloaded PDF
- `paper-arxiv-2604.15804-pdf_md.md` — Markdown extracted from PDF via `read_pdf`

`<id>` must remain in `xxxx.xxxxx` form when available.

## Workflow

### 1) Resolve URL

Default HTML URL:
- `https://arxiv.org/html/<id>`

If the user explicitly gives a versioned URL like `https://arxiv.org/html/2604.15804v1`, preserve it for fetching, but keep output filenames based on the base ID `2604.15804` unless the user asks otherwise.

### 2) Fetch and save source artifacts

Create at minimum:
- raw HTML
- raw extracted text
- info JSON with metadata such as title, source URL, fetched time, file list

Use the naming scheme above.

### 3) Convert HTML to Markdown with the bundled Python script

Use `scripts/arxiv_html_to_md.py`.

Expected behavior:
- parse LaTeXML/ar5iv-like arXiv HTML,
- keep title, abstract, and body sections,
- exclude obvious chrome/noise such as TOC, header, modal, infobox, navigation,
- exclude `Authors` section by default,
- emit Markdown.

### 3b) Download PDF from arXiv

arXiv PDF URL format:
- `https://arxiv.org/pdf/<id>`
- Versioned: `https://arxiv.org/pdf/<id>vN`

Use `download_file(url, dest=paper-arxiv-<id>.pdf)` to download.

If the file already exists and the user has not requested a forced re-download, skip this step.

### 3c) Extract PDF content via read_pdf

Call `read_pdf(path="paper-arxiv-<id>.pdf")` to extract text as Markdown.

Expected outputs:
- `outline`: chapter/section headings mapped to line numbers in the extracted Markdown
- `md_path`: path to cached Markdown file (save/copy as `paper-arxiv-<id>-pdf_md.md`)
- `page_count`: total pages

Use the outline for targeted reading—navigate to specific sections by line number via `read_file(offset=...)`.

**Cross-validation**: Compare HTML-extracted content against PDF-extracted content. If discrepancies exist (e.g., missing figures, garbled equations, truncated sections), prefer PDF extraction. The PDF is the authoritative source for equation rendering and figure captions; HTML excels at structured text navigation.

**Large PDFs**: If the PDF has >30 pages, use the outline to read only the Introduction, Method, Experiments, and Conclusion sections rather than the entire document.

### 4) Summarize the paper in this structure

> When PDF content is available, cross-validate key claims (metrics, architecture details, dataset sizes) against the HTML version. Prefer PDF values when discrepancies exist.

The summary should explicitly answer:

#### A. Problem and pain points
- What problem does the paper identify?
- What are the current pain points?
- What existing solution families are discussed?
- What are their shortcomings?

#### B. Method and advantages
- What method do the authors propose?
- What are the key components?
- Why is it better or more suitable than prior approaches?

#### C. Experiments and conclusions
For each major experiment block, extract:
- what was compared,
- which datasets / benchmarks / settings were used,
- what conclusion was obtained.

Prefer a sectioned Markdown report rather than a loose paragraph.

## Recommended extraction targets from arXiv HTML

Primary container:
- `article.ltx_document`

Primary text tags:
- `div#abstract1.ltx_abstract`
- `section.ltx_section`
- `section.ltx_subsection`
- `section.ltx_subsubsection`
- `div.ltx_para`
- `p.ltx_p`

Optional supportive content:
- `figcaption.ltx_caption`
- `caption.ltx_caption`
- `.ltx_equation`, `.ltx_equationgroup`, `math.ltx_Math`

Exclude noise when possible:
- `header.arxiv-html-header`
- `nav.ltx_page_navbar`
- `nav.ltx_TOC`
- `ol.ltx_toclist`
- `.ltx_tocentry`
- `div#infobox.infobox`
- `div.ltx_authors`
- `dialog#modal-form`
- `.ltx_pagination.ltx_role_newpage`

## Bundled resources

- Script: `scripts/arxiv_html_to_md.py`
- Reference: `references/summary-template.md`
- Reference: `references/pdf-extraction-notes.md`

Read the reference files when writing the final paper summary. `pdf-extraction-notes.md` contains guidelines for using `read_pdf` effectively and handling common extraction issues.
