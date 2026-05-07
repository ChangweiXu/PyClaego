---
name: hf-daily-papers
description: Fetch and organize Hugging Face Daily Papers for a specified date. Downloads raw HTML, extracts the embedded JSON data blob (Svelte hydration data in data-props attribute) with a Python script, then produces a structured Markdown report with all arXiv IDs, upvote counts, comment counts, GitHub stars, organizations, AI summaries, and keywords. Use when asked to grab, scrape, summarize, classify, or export Hugging Face daily papers. Also use when the user wants the result saved as a Markdown report grouped by research domains.
---

# Hugging Face Daily Papers

Fetch Hugging Face Daily Papers for a target date, extract per-paper full metadata from the embedded Svelte hydration JSON, classify papers by domain, and save the result as a Markdown + JSON report.

## When to use

Use this skill when the user asks to:
- 抓取某天的 Hugging Face 每日论文列表
- 获取某天 Daily Papers 的标题、点赞量、arXiv ID、评论数、GitHub Star 等
- 按领域分类输出论文清单
- 将结果保存为 Markdown 文档

## Critical insight

The HF Daily Papers page is a **Svelte SPA**. The server-rendered HTML does **not** contain human-readable paper data. Instead, all paper metadata lives in a single JSON blob inside a `data-props` attribute:

```html
<div class="SVELTE_HYDRATER contents"
     data-target="DailyPapers"
     data-props='{"dailyPapers":[{...21 papers...}], ...}'>
```

**Do NOT use `web_fetch` for this page.** The Markdown/text conversion loses arXiv IDs, author links, and structured metadata. Instead, use `download_file` to get the raw HTML, then extract JSON with Python (see below).

## Workflow

### 1) Determine the target date

- Default to yesterday if no date specified.
- The page uses format: `https://huggingface.co/papers/date/YYYY-MM-DD`
- Daily Papers are updated on workdays only. If the user requests a weekend date, warn that the list may not have been updated.

### 2) Download raw HTML

Use `download_file` (NOT `web_fetch`):

```
download_file(
  url="https://huggingface.co/papers/date/YYYY-MM-DD",
  dest="hf_papers_YYYY-MM-DD.html",
  overwrite=true
)
```

This preserves the full DOM including the `data-props` JSON blob.

### 3) Extract JSON with Python

Use the bundled script or the following inline Python pattern:

```python
import json, re

with open("hf_papers_YYYY-MM-DD.html", "r", encoding="utf-8") as f:
    html = f.read()

# Locate the JSON blob inside data-props
dp_start = html.rfind('data-props="', 0, html.find('"dailyPapers"') + 50000)
json_start = dp_start + len('data-props="')
json_end = html.find('"><section', json_start)

json_str = html[json_start:json_end]
json_str = json_str.replace("&quot;", '"').replace("&amp;", "&")

data = json.loads(json_str)
papers = data["dailyPapers"]
```

### 4) Extract per-paper fields

From each entry in `data.dailyPapers[i]`:

| Field | Path | Example |
|-------|------|------|
| arXiv ID | `paper.id` | `"2605.00658"` |
| Title | `title` (or `paper.title`) | `"UniVidX: A Unified..."` |
| Upvotes | `paper.upvotes` | `71` |
| Comments | `numComments` | `2` |
| GitHub Stars | `paper.githubStars` | `52` (may be absent) |
| Organization | `organization.name` | `"ByteDance"` (may be absent) |
| HF URL | `https://huggingface.co/papers/{paper.id}` | — |
| arXiv URL | `https://arxiv.org/abs/{paper.id}` | — |
| AI Summary | `paper.ai_summary` | Short one-liner |
| Keywords | `paper.ai_keywords` | Array of strings |
| Authors | `paper.authors[].name` | Array |
| Author participation | `isAuthorParticipating` | `true`/`false` |
| Submitted by | `submittedBy.fullname` | `"taesiri"` |
| Thumbnail | `thumbnail` | Image URL |

### 5) Save output files

Save both a structured JSON and a polished Markdown report:

| File | Format | Purpose |
|------|--------|------|
| `hf_daily_YYYY-MM-DD.json` | JSON | Machine-readable, full metadata |
| `hf_daily_YYYY-MM-DD.md` | Markdown | Human-readable report, domain-classified |

### 6) Classify by domain

Use the paper's primary contribution to assign one main domain. Common domains include:
- Agent / AI Systems / Tool Use / RAG
- Reinforcement Learning / Reward Modeling
- LLM Training / Distillation / Inference Efficiency / Safety
- Vision-Language / Multimodal / Robotics
- 3D / Graphics / World Models / Video Generation
- Memory / Cognitive Architectures
- Continual / Incremental Learning
- Healthcare / Biology
- Benchmarks / Evaluation

**Each paper gets exactly one domain.** Total entries must equal the total paper count.

## Output structure (Markdown)

```md
# 🤗 Hugging Face Daily Papers — YYYY-MM-DD

**共 N 篇论文** | 数据来源: https://huggingface.co/papers/date/YYYY-MM-DD

| # | 👍 | arXiv ID | 论文标题 | 💬 | ⭐ | 机构 |
|:--:|:--:|:--|------|:--:|:--:|------|
| 1 | 71 | `2605.00658` | [UniVidX: ...](https://arxiv.org/abs/2605.00658) | 2 | 52 | — |

---

## 论文速览

### 1. Paper Title
**arXiv:** `2605.xxxxx` | **👍 71** | **🏢 Organization**

> AI summary one-liner

**关键词:** kw1 · kw2 · kw3
---
```

## Quality bar

- Do NOT invent arXiv IDs. Every ID must come from the JSON.
- Do NOT use `web_fetch` for this task — use `download_file` + Python.
- Verify the paper count: `len(papers)` must equal 21 (or the day's actual count).
- Keep each overview concise and factual.
- Preserve the original paper title verbatim.
- Cross-check: the total entries in the Markdown table must equal the paper count in the header.

## Update cadence

- Hugging Face Daily Papers are typically updated on workdays only.
- If the user requests a weekend date, first check whether a date page exists. If not, report that the list may not have been updated that day and ask whether to use the previous workday.

## Bundled resources

- Reference: `references/extraction-notes.md` — detailed extraction method and edge cases

Read `references/extraction-notes.md` for the full Python extraction script and troubleshooting guide.
