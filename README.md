# Translated RSS on GitHub Pages

This repository builds a Chinese-friendly RSS feed from RSS and sitemap sources.

Generated files:

- `feed.xml`: subscribe to this in Inoreader, Feedly, FreshRSS, etc.
- `daily.md`: Markdown digest.
- `items.json`: structured data for debugging or reuse.

Pipeline:

```text
RSS / Sitemap
-> fetch title, link, date, summary
-> optional AI translation and Chinese summary
-> feed.xml + daily.md
-> GitHub Pages
```

## Setup

1. Open the repository on GitHub.
2. Go to `Settings` -> `Pages`.
3. Set `Build and deployment` -> `Source` to `GitHub Actions`.
4. Go to `Settings` -> `Secrets and variables` -> `Actions`.
5. Add repository secret `AI_API_KEY`. Put only the API key value in this Secret. Do not include the `Bearer ` prefix.
6. No repository variable is required for `AI_MODEL` or `AI_BASE_URL`; the workflow already uses the Baishan/OpenAI-compatible defaults.
7. Optional: add repository variable `AI_DELAY_SECONDS` if you want to slow down requests.
8. Go to `Actions` and run `Build translated RSS and deploy Pages`, or push a new commit to `main`.

After deployment, subscribe to:

```text
https://<your-github-username>.github.io/<repo-name>/feed.xml
```

Markdown digest:

```text
https://<your-github-username>.github.io/<repo-name>/daily.md
```

## Configure Sources

Edit `config/sources.json`.

RSS source:

```json
{
  "name": "Engineering.com",
  "enabled": true,
  "type": "rss",
  "url": "https://www.engineering.com/feed/",
  "max_items": 10
}
```

Sitemap source:

```json
{
  "name": "DPIT",
  "enabled": true,
  "type": "sitemap",
  "url": "https://dpit.lib00.com/sitemap.xml",
  "include_any": ["/zh/content/"],
  "max_items": 6
}
```

`include_any` and `exclude_any` are simple substring filters.

The Machine Heart source is included as a disabled example. Its sitemap is reachable, but many direct article fetches currently return a data-service landing page, so it needs a site-specific parser or another upstream feed before enabling.

## AI Translation

The script uses an OpenAI-compatible Chat Completions API:

```text
POST {AI_BASE_URL}/chat/completions
Authorization: Bearer ${AI_API_KEY}
```

Environment variables:

```text
AI_API_KEY       GitHub Secret. Required for hosted providers.
AI_BASE_URL      Set in the workflow: https://api.edgefn.net/v1.
AI_MODEL         Set in the workflow: DeepSeek-R1-0528-Qwen3-8B.
AI_DELAY_SECONDS GitHub Variable. Defaults to 12.
```

This repository is configured for Baishan/OpenAI-compatible chat completions by default:

```text
AI_BASE_URL=https://api.edgefn.net/v1
AI_MODEL=DeepSeek-R1-0528-Qwen3-8B
```

If old repository variables still exist from another provider, this workflow ignores them for `AI_BASE_URL` and `AI_MODEL`.

If `AI_API_KEY` is not set, the feed is still generated, but English items stay mostly untranslated and are marked as fallback.

The parser accepts plain JSON, fenced JSON, and outputs with R1-style `<think>...</think>` reasoning before the JSON. The final content still needs to contain one JSON object with these fields:

```json
{
  "title_zh": "中文标题",
  "topic_zh": "主题",
  "summary_zh": "中文简介",
  "keywords_zh": ["关键词1", "关键词2"],
  "relevance": 8
}
```

## Local Test

Without AI:

```powershell
python scripts/build_feed.py
```

With AI:

```powershell
$env:AI_API_KEY="your-key"
$env:AI_BASE_URL="https://api.edgefn.net/v1"
$env:AI_MODEL="DeepSeek-R1-0528-Qwen3-8B"
python scripts/build_feed.py
```

Output:

```text
public/feed.xml
public/daily.md
public/items.json
public/index.html
```

## Schedule

The workflow runs on push to `main` and every 6 hours:

```yaml
- cron: "17 */6 * * *"
```

Adjust it in `.github/workflows/build-feed.yml`.
