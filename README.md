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
5. Add repository secret `AI_API_KEY`.
6. Add repository variable `AI_MODEL`, for example `gpt-4o-mini`.
7. Optional: add repository variable `AI_BASE_URL` for an OpenAI-compatible provider.
8. Go to `Actions` and run `Build translated RSS and deploy Pages`.

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

The script uses an OpenAI-compatible Chat Completions API.

Environment variables:

```text
AI_API_KEY    required for hosted providers
AI_BASE_URL   optional, defaults to https://api.openai.com/v1
AI_MODEL      optional, defaults to gpt-4o-mini
```

If `AI_API_KEY` is not set, the feed is still generated, but English items stay mostly untranslated and are marked as fallback.

## Local Test

Without AI:

```powershell
python scripts/build_feed.py
```

With AI:

```powershell
$env:AI_API_KEY="your-key"
$env:AI_MODEL="gpt-4o-mini"
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

The workflow runs every 6 hours:

```yaml
- cron: "17 */6 * * *"
```

Adjust it in `.github/workflows/build-feed.yml`.
