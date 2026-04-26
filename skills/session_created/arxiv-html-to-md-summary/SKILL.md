---
name: arxiv-html-to-md-summary
description: Given an arXiv ID like 2604.15804, fetch the arXiv HTML page, save raw HTML/text/info files with the prefix paper-arxiv-<id>-<doc type>.*, convert the HTML body into Markdown via a bundled Python script, then produce a structured paper summary covering problem/pain points, proposed method/advantages, and experiments/comparisons/conclusions. Use when the user asks to read, parse, summarize, or export an arXiv paper from its arXiv ID or arXiv HTML page.
---

# arXiv HTML to Markdown + Summary

Use this skill when the user provides an arXiv ID and wants a reproducible workflow that:
1. resolves the arXiv HTML page,
2. downloads and stores source artifacts,
3. converts HTML to Markdown with Python,
4. summarizes the paper in a fixed structure.

## Required file naming

All generated files must use this prefix:
- `paper-arxiv-<id>-<doc type>.<ext>`

Examples for id `2604.15804`:
- `paper-arxiv-2604.15804-raw_html.html`
- `paper-arxiv-2604.15804-raw_text.txt`
- `paper-arxiv-2604.15804-info.json`
- `paper-arxiv-2604.15804-md.md`
- `paper-arxiv-2604.15804-summary.md`

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

### 4) Summarize the paper in this structure

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

Read the reference file when writing the final paper summary.
