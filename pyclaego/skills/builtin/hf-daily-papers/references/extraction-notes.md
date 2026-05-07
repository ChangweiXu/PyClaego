# Extraction Notes for Hugging Face Daily Papers

## Why download_file + Python, not web_fetch?

| Approach | Result |
|----------|--------|
| `web_fetch(url, format="text")` | ❌ All hyperlinks stripped → arXiv IDs lost |
| `web_fetch(url, format="md")` | ❌ SPA rendering incomplete → titles missing, JSON invisible |
| `download_file(url, ...)` + Python | ✅ Raw HTML preserved → full JSON accessible |

The HF Daily Papers page is a **Svelte SPA**. The data lives in:
```html
data-target="DailyPapers"
data-props='{"dailyPapers":[...21 papers...]}'
```

This JSON is invisible to `web_fetch`'s text/Markdown converters. Only raw HTML download preserves it.

## Complete Python extraction script

```python
import json, os

HTML_PATH = "hf_papers_YYYY-MM-DD.html"

with open(HTML_PATH, "r", encoding="utf-8") as f:
    html = f.read()

# Step 1: Locate the data-props JSON
# The JSON contains "dailyPapers" as a key — find that first
dp_start = html.rfind('data-props="', 0, html.find('"dailyPapers"') + 50000)
json_start = dp_start + len('data-props="')

# The JSON ends at the next attribute boundary: "><section
json_end = html.find('"><section', json_start)

json_str = html[json_start:json_end]

# Step 2: Unescape HTML entities inside the JSON
json_str = json_str.replace("&quot;", '"').replace("&amp;", "&")

# Step 3: Parse
data = json.loads(json_str)
papers = data["dailyPapers"]

print(f"Date: {data['dateString']}")
print(f"Papers: {len(papers)}")

# Step 4: Extract fields
for i, entry in enumerate(papers, 1):
    p = entry.get("paper", {})
    paper_id = p.get("id", "N/A")
    title = entry.get("title", p.get("title", "N/A"))
    upvotes = p.get("upvotes", 0)
    comments = entry.get("numComments", 0)
    github_stars = p.get("githubStars", "—")
    org = entry.get("organization", {}).get("name", "")
    ai_summary = p.get("ai_summary", "")
    keywords = p.get("ai_keywords", [])
    authors = [a.get("name", "") for a in p.get("authors", [])]
    submitted_by = entry.get("submittedBy", {}).get("fullname", "")
    is_author = entry.get("isAuthorParticipating", False)

    print(f"{i:2d}. [{paper_id}] {title}")
    print(f"    👍 {upvotes} | 💬 {comments} | ⭐ {github_stars}")
    if org:
        print(f"    🏢 {org}")
    if ai_summary:
        print(f"    📝 {ai_summary}")

# Step 5: Save JSON
out_json = f"hf_daily_{data['dateString']}.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

# Step 6: Save Markdown report
# (See SKILL.md for the Markdown template)
```

## Edge cases

| Issue | Solution |
|-------|----------|
| `rfind` returns wrong position | Ensure the search range for `data-props=` is large enough (>50000 chars before `dailyPapers`) |
| JSON parse error | Check for unescaped `&` or other HTML entities; replace `&amp;` before parsing |
| Weekend date | The page may return fewer papers or be empty; check `dateString` in the response |
| Paper count ≠ 21 | Normal — some days have more or fewer papers; use the actual count |
| `web_fetch` was used instead of `download_file` | The JSON will be invisible; re-download with `download_file` |
| GitHub stars missing | Use `"—"` as fallback; the field is optional in the API |
| Organization missing | Use `""` as fallback; not all papers have an organization |
| Double-escaped backslashes in titles | Some titles contain `\\\\` — strip extra backslashes in output |

## Field reference

```
data.dailyPapers[i]              — paper entry
  .paper.id                      — arXiv ID (e.g., "2605.00658")
  .paper.upvotes                 — like count (int)
  .paper.githubStars             — GitHub stars (int or absent)
  .paper.ai_summary              — one-line AI summary
  .paper.ai_keywords             — array of keywords
  .paper.authors[].name          — author name
  .paper.authors[].user.avatarUrl — author avatar
  .paper.summary                 — full paper abstract
  .title                         — display title
  .numComments                   — comment count
  .organization.name             — org name (may be absent)
  .organization.fullname         — org full name
  .isAuthorParticipating         — bool: author in discussion
  .submittedBy.fullname          — who submitted
  .thumbnail                     — social thumbnail URL
  .publishedAt                   — ISO date string
```

## Verifying extraction integrity

After extraction, run these checks:

1. `len(papers)` should be 21 (or the day's actual count)
2. Every entry must have a non-null `paper.id`
3. Every `paper.id` must match `^\\d{4}\\.\\d{5}$` (arXiv ID format)
4. Sum of upvotes should be > 0
5. No duplicate `paper.id` values
