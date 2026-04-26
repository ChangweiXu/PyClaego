---
name: hf-daily-papers
description: Fetch and organize Hugging Face Daily Papers for a specified date. Use when asked to grab, scrape, summarize, classify, or export Hugging Face daily papers, especially when the user mentions a date, weekday, daily paper list, arXiv IDs, or Hugging Face paper detail pages. Also use when the user wants the result saved as a Markdown report grouped by research domains.
---

# Hugging Face Daily Papers

Fetch Hugging Face Daily Papers for a target date, extract per-paper identifiers and metadata, classify papers by domain, and save the result as a Markdown report.

## When to use

Use this skill when the user asks to:
- 抓取某天的 Hugging Face 每日论文列表
- 获取某天 Daily Papers 的标题、点赞量、链接、摘要
- 提取每篇论文的 arXiv ID
- 按领域分类输出论文清单
- 将结果保存为 Markdown 文档

## Workflow

1. Determine the target date.
2. Prefer the date page format:
   - `https://huggingface.co/papers/date/YYYY-MM-DD`
3. Fetch the page.
4. Extract each paper's:
   - title
   - arXiv ID
   - Hugging Face paper URL
   - like/upvote count
   - concise overview
5. Classify papers by the main research domain.
6. Save the final report as Markdown using the required naming format.

## Required facts

### Update cadence
- Hugging Face Daily Papers are typically updated on workdays only.
- If the user requests a weekend date, first check whether a date page exists. If not, report that the list may not have been updated that day and ask whether to use the previous workday.

### Date page URL format
- Daily page:
  - `https://huggingface.co/papers/date/YYYY-MM-DD`
- Main page:
  - `https://huggingface.co/papers`
- Weekly page example:
  - `https://huggingface.co/papers/week/YYYY-Www`

### How to get the arXiv ID
Use one of these sources, in this order:

1. Inspect the date page HTML and extract the paper link:
   - pattern: `/papers/[arxiv_id]`
2. Inspect embedded front-end data in the HTML and read:
   - `paper.id`
3. If needed, follow the paper detail page and confirm the arXiv identifier there.

The arXiv ID is usually the same identifier used in the Hugging Face paper detail URL.

### Hugging Face paper detail page URL format
- `https://huggingface.co/papers/[arxiv_id]`

### arXiv URL format
- `https://arxiv.org/abs/[arxiv_id]`

## Output requirements

### File naming
Save the Markdown file using this exact pattern:
- `hf-daily-paper-YYYYMMDD-[count].md`

Where:
- `YYYYMMDD` is the target date
- `count` is the number of papers captured for that day

### Output structure
Organize the report by research domain. For each paper, include:
- title
- like/upvote count
- arXiv link
- Hugging Face link
- concise content overview

### Recommended Markdown structure

```md
# Hugging Face Daily Papers (YYYY-MM-DD)

来源：<date-page-url>
总数：N

## Domain A

### Paper Title
- 点赞量：123
- arXiv：<https://arxiv.org/abs/xxxx.xxxxx>
- Hugging Face：<https://huggingface.co/papers/xxxx.xxxxx>
- 概述：一句到两句简介

## Domain B
...
```

## Classification guidance

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

If a paper spans multiple areas, choose exactly one dominant domain and assign the paper only once in the whole report. Do not duplicate the same paper under multiple domains. The total number of paper entries across all domains must equal the captured paper count in the filename and report header.

## Extraction notes

- Prefer the raw HTML over plain extracted text when you need exact links or IDs.
- The visible text may omit arXiv IDs even when the HTML contains them.
- The visible page includes like counts near each title; verify carefully when parsing.
- If the page is truncated by the fetch tool, use the cached file or fetch the raw HTML and inspect it.

## Quality bar

- Do not invent arXiv IDs.
- Do not infer missing links without clear evidence from page structure.
- Keep each overview concise and factual.
- Preserve the original paper title verbatim.
- Ensure the filename count matches the actual number of listed papers.

## References

Read `references/extraction-notes.md` when you need concrete extraction hints or parsing reminders.
