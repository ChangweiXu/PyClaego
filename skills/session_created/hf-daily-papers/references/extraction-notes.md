# Extraction Notes for Hugging Face Daily Papers

## Primary target pages
- Daily page: `https://huggingface.co/papers/date/YYYY-MM-DD`
- Detail page: `https://huggingface.co/papers/[arxiv_id]`

## Reliable extraction signals

1. Paper detail links in HTML
   - Search for `/papers/` links
   - Common target pattern: `/papers/[arxiv_id]`

2. Embedded page data
   - The page HTML may contain structured data with fields such as:
     - `paper.id`
     - `paper.title`

3. Like counts
   - Usually shown in the visible page near each paper card/title.
   - When scraping from plain text, confirm alignment carefully before pairing likes with titles.

## Operational reminders
- Daily Papers are commonly weekday-updated. Weekend requests may need fallback confirmation.
- Prefer date pages over the homepage when the user specifies a date.
- If the fetch tool truncates HTML/text, inspect the cached raw file or narrow the extraction target.
- Save output filename as: `hf-daily-paper-YYYYMMDD-[count].md`

## Output checklist
For each paper:
- title
- like/upvote count
- arXiv URL
- Hugging Face URL
- concise overview
- assigned primary domain
