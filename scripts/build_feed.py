import datetime as dt
import email.utils
import gzip
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path


CONFIG_PATH = Path(os.getenv("CONFIG_PATH") or "config/sources.json")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR") or "public")
CACHE_DIR = Path(os.getenv("CACHE_DIR") or ".cache")
AI_CACHE_PATH = CACHE_DIR / "ai-cards.json"


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


FETCH_WORKERS = env_int("FETCH_WORKERS", 6)
MAX_TOTAL_ITEMS = env_int("MAX_TOTAL_ITEMS", 40)
PAGE_FETCH_TIMEOUT = env_int("PAGE_FETCH_TIMEOUT", 18)
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 translated-rss-generator/1.0",
)

AI_API_KEY = os.getenv("AI_API_KEY") or ""
AI_BASE_URL = (
    os.getenv("AI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai/"
).rstrip("/")
AI_MODEL = os.getenv("AI_MODEL") or "gemini-flash-latest"
AI_TIMEOUT = env_int("AI_TIMEOUT", 45)
AI_MAX_INPUT_CHARS = env_int("AI_MAX_INPUT_CHARS", 3500)
AI_DELAY_SECONDS = env_int("AI_DELAY_SECONDS", 12)
AI_ENABLED = (os.getenv("AI_ENABLED") or "auto").lower()


class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title_parts = []
        self.description = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = {k.lower(): v for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        if tag == "meta" and attrs_dict.get("name", "").lower() == "description":
            self.description = attrs_dict.get("content", "") or ""
        if tag == "meta" and attrs_dict.get("property", "").lower() == "og:description":
            self.description = self.description or attrs_dict.get("content", "") or ""

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self):
        return normalize_space(" ".join(self.title_parts))


class BodyTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "nav", "footer"}

    def __init__(self):
        super().__init__()
        self.skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "h1", "h2", "h3", "li"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if self.skip_depth == 0:
            text = normalize_space(data)
            if text:
                self.parts.append(text)

    @property
    def text(self):
        return normalize_space(" ".join(self.parts))


def normalize_space(value):
    value = html.unescape(value or "")
    return re.sub(r"\s+", " ", value).strip()


def strip_tags(value):
    parser = BodyTextParser()
    try:
        parser.feed(value or "")
        return parser.text
    except Exception:
        return normalize_space(re.sub(r"<[^>]+>", " ", value or ""))


def truncate(value, limit):
    value = normalize_space(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node, names):
    names = {name.lower() for name in names}
    for child in list(node):
        if local_name(child.tag) in names and child.text:
            return normalize_space(child.text)
    return ""


def child_attr(node, child_name, attr_name):
    for child in list(node):
        if local_name(child.tag) == child_name.lower():
            value = child.attrib.get(attr_name)
            if value:
                return normalize_space(value)
    return ""


def fetch_bytes(url, timeout=20):
    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                return response.read(), content_type
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404, 410}:
                raise
        except Exception as exc:
            last_error = exc
        time.sleep(1 + attempt)
    try:
        result = subprocess.run(
            [
                "curl",
                "--location",
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                str(timeout),
                "--user-agent",
                USER_AGENT,
                "--header",
                "Accept: text/html,application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
                url,
            ],
            check=True,
            capture_output=True,
        )
        return result.stdout, ""
    except Exception:
        pass
    raise last_error


def decode_bytes(data, content_type="", url=""):
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    charset_match = re.search(r"charset=([^;\s]+)", content_type or "", re.I)
    charsets = ["utf-8"]
    if charset_match:
        charsets.insert(0, charset_match.group(1).strip('"'))
    for charset in charsets:
        try:
            return data.decode(charset)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def fetch_text(url, timeout=20):
    data, content_type = fetch_bytes(url, timeout=timeout)
    return decode_bytes(data, content_type, url=url)


def parse_datetime(value):
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    value = normalize_space(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed_date = dt.date.fromisoformat(value[:10])
            parsed = dt.datetime.combine(parsed_date, dt.time.min)
        except ValueError:
            return dt.datetime.now(dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def rss_date(value):
    return email.utils.format_datetime(value.astimezone(dt.timezone.utc))


def clean_url(url):
    url = html.unescape(normalize_space(url))
    nested_https = url.rfind("https://")
    if nested_https > 0:
        url = url[nested_https:]
    else:
        nested_http = url.rfind("http://")
        if nested_http > 0:
            url = url[nested_http:]
    return url


def url_allowed(url, source):
    include_any = source.get("include_any") or []
    exclude_any = source.get("exclude_any") or []
    if include_any and not any(part in url for part in include_any):
        return False
    if exclude_any and any(part in url for part in exclude_any):
        return False
    return True


def slug_title(url):
    path = urllib.parse.urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] or url
    slug = re.sub(r"^\d+[-_]?", "", slug)
    slug = re.sub(r"[-_]+", " ", slug)
    return slug.strip().title() or url


def parse_feed_source(source):
    text = fetch_text(source["url"], timeout=PAGE_FETCH_TIMEOUT)
    root = ET.fromstring(text)
    items = []

    if local_name(root.tag) == "rss":
        channel = root.find("channel")
        if channel is None:
            return []
        nodes = channel.findall("item")
        for node in nodes:
            link = child_text(node, {"link"}) or child_text(node, {"guid"})
            if not link:
                continue
            link = clean_url(link)
            description = child_text(node, {"description"}) or child_text(node, {"encoded"})
            item = {
                "source": source["name"],
                "source_url": source["url"],
                "title": child_text(node, {"title"}) or slug_title(link),
                "link": link,
                "updated": parse_datetime(child_text(node, {"pubdate", "updated", "date"})),
                "raw_summary": truncate(strip_tags(description), 800),
                "excerpt": truncate(strip_tags(description), 1500),
            }
            items.append(item)

    elif local_name(root.tag) == "feed":
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}", 1)[0] + "}"
        for node in root.findall(f"{ns}entry"):
            link = child_attr(node, "link", "href") or child_text(node, {"link", "id"})
            if not link:
                continue
            link = clean_url(link)
            description = child_text(node, {"summary", "content"})
            item = {
                "source": source["name"],
                "source_url": source["url"],
                "title": child_text(node, {"title"}) or slug_title(link),
                "link": link,
                "updated": parse_datetime(child_text(node, {"updated", "published"})),
                "raw_summary": truncate(strip_tags(description), 800),
                "excerpt": truncate(strip_tags(description), 1500),
            }
            items.append(item)

    return [item for item in items if url_allowed(item["link"], source)]


def parse_sitemap_xml(xml_text, source, depth=0):
    root = ET.fromstring(xml_text)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"

    if local_name(root.tag) == "sitemapindex" and depth < 2:
        nested_items = []
        for node in root.findall(f"{ns}sitemap"):
            loc = child_text(node, {"loc"})
            if not loc:
                continue
            try:
                nested_text = fetch_text(clean_url(loc), timeout=PAGE_FETCH_TIMEOUT)
                nested_items.extend(parse_sitemap_xml(nested_text, source, depth + 1))
            except Exception as exc:
                print(f"Warning: failed to fetch nested sitemap {loc}: {exc}", file=sys.stderr)
        return nested_items

    items = []
    for node in root.findall(f"{ns}url"):
        loc = child_text(node, {"loc"})
        if not loc:
            continue
        link = clean_url(loc)
        if not url_allowed(link, source):
            continue
        items.append(
            {
                "source": source["name"],
                "source_url": source["url"],
                "title": slug_title(link),
                "link": link,
                "updated": parse_datetime(child_text(node, {"lastmod"})),
                "raw_summary": "",
                "excerpt": "",
            }
        )
    return items


def parse_sitemap_source(source):
    text = fetch_text(source["url"], timeout=PAGE_FETCH_TIMEOUT)
    return parse_sitemap_xml(text, source)


def extract_page_info(item):
    if item.get("raw_summary") and item.get("title"):
        return item
    try:
        page = fetch_text(item["link"], timeout=PAGE_FETCH_TIMEOUT)
        head = HeadParser()
        head.feed(page[:100000])
        body = BodyTextParser()
        body.feed(page[:250000])
        item["title"] = head.title or item["title"] or slug_title(item["link"])
        item["raw_summary"] = truncate(head.description or body.text, 800)
        item["excerpt"] = truncate(body.text or head.description, 1800)
    except Exception as exc:
        print(f"Warning: failed to fetch page {item['link']}: {exc}", file=sys.stderr)
        item["title"] = item.get("title") or slug_title(item["link"])
        item["raw_summary"] = item.get("raw_summary") or item["link"]
        item["excerpt"] = item.get("excerpt") or item["raw_summary"]
    return item


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        config = json.load(fh)
    config.setdefault("site", {})
    config.setdefault("sources", [])
    config.setdefault("settings", {})
    return config


def collect_items(config):
    collected = []
    enabled_sources = [source for source in config["sources"] if source.get("enabled", True)]

    for source in enabled_sources:
        try:
            source_type = source.get("type", "rss").lower()
            if source_type == "rss":
                items = parse_feed_source(source)
            elif source_type == "sitemap":
                items = parse_sitemap_source(source)
            else:
                print(f"Warning: unknown source type {source_type} for {source['name']}", file=sys.stderr)
                continue
            items.sort(key=lambda item: item["updated"], reverse=True)
            per_source_max = int(source.get("max_items") or config["settings"].get("max_items_per_source") or 15)
            collected.extend(items[:per_source_max])
            print(f"Loaded {min(len(items), per_source_max)} items from {source['name']}")
        except Exception as exc:
            print(f"Warning: failed source {source.get('name', source.get('url'))}: {exc}", file=sys.stderr)

    seen = set()
    deduped = []
    for item in sorted(collected, key=lambda item: item["updated"], reverse=True):
        link = item["link"]
        if link in seen:
            continue
        seen.add(link)
        deduped.append(item)
    return deduped[:MAX_TOTAL_ITEMS]


def ai_is_available():
    if AI_ENABLED in {"0", "false", "no", "off"}:
        return False
    supplied_base_url = bool(os.getenv("AI_BASE_URL"))
    return bool(AI_API_KEY or supplied_base_url)


def cache_key(item):
    payload = {
        "model": AI_MODEL,
        "title": item.get("title", ""),
        "summary": item.get("raw_summary", ""),
        "excerpt": item.get("excerpt", "")[:AI_MAX_INPUT_CHARS],
        "link": item.get("link", ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_ai_cache():
    if not AI_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AI_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ai_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    AI_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_json_from_text(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def call_ai(item):
    system_prompt = (
        "\u4f60\u662f\u4e00\u4e2a\u4e2d\u6587\u4fe1\u606f\u6d41\u7f16\u8f91\u3002\u6839\u636e\u539f\u6587\u6807\u9898\u3001\u6765\u6e90\u3001\u6458\u8981\u548c\u6b63\u6587\u7247\u6bb5\uff0c"
        "\u751f\u6210\u9002\u5408 RSS \u5feb\u901f\u6d4f\u89c8\u7684\u4fe1\u606f\u5361\u3002\u53ea\u8f93\u51fa JSON\uff0c\u4e0d\u8981\u8f93\u51fa Markdown\u3002"
    )
    user_payload = {
        "source": item["source"],
        "url": item["link"],
        "title": item["title"],
        "summary": item.get("raw_summary", ""),
        "excerpt": truncate(item.get("excerpt", ""), AI_MAX_INPUT_CHARS),
        "requirements": {
            "title_zh": "\u81ea\u7136\u3001\u51c6\u786e\u7684\u4e2d\u6587\u6807\u9898\uff1b\u5982\u679c\u539f\u6587\u5df2\u7ecf\u662f\u4e2d\u6587\uff0c\u4e5f\u8981\u6da6\u8272\u5f97\u66f4\u6e05\u695a",
            "topic_zh": "2\u52308\u4e2a\u5b57\u7684\u4e3b\u9898\uff0c\u4f8b\u5982 AI Agent\u3001\u5236\u9020\u4e1a\u3001\u673a\u5668\u4eba\u3001CAD\u3001\u673a\u5668\u5b66\u4e60",
            "summary_zh": "80\u5230140\u5b57\u4e2d\u6587\u7b80\u4ecb\uff0c\u8bf4\u660e\u6587\u7ae0\u6838\u5fc3\u89c2\u70b9\u548c\u4e3a\u4ec0\u4e48\u503c\u5f97\u770b",
            "keywords_zh": "3\u52306\u4e2a\u4e2d\u6587\u5173\u952e\u8bcd",
            "relevance": "1\u523010\u7684\u6574\u6570\uff0c\u8868\u793a\u5bf9\u5de5\u7a0b/AI/\u6280\u672f\u8d44\u8baf\u8bfb\u8005\u7684\u76f8\u5173\u5ea6",
        },
    }
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }

    headers = {"Content-Type": "application/json"}
    if AI_API_KEY:
        headers["Authorization"] = f"Bearer {AI_API_KEY}"

    url = f"{AI_BASE_URL}/chat/completions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    for attempt in range(1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            content = response_data["choices"][0]["message"]["content"]
            return parse_json_from_text(content)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AI request failed: HTTP {exc.code} {body[:500]}") from exc


def fallback_card(item, reason="not_configured"):
    text = item.get("raw_summary") or item.get("excerpt") or item["link"]
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", item.get("title", "") + text))
    if has_chinese:
        summary = truncate(text, 140)
    else:
        prefix = "\u672a\u914d\u7f6e AI \u7ffb\u8bd1\uff0c\u6682\u663e\u793a\u539f\u6587\u6458\u8981\uff1a"
        if reason == "failed":
            prefix = "AI \u5904\u7406\u5931\u8d25\uff0c\u6682\u663e\u793a\u539f\u6587\u6458\u8981\uff1a"
        summary = prefix + truncate(text, 120)
    return {
        "title_zh": item.get("title") or slug_title(item["link"]),
        "topic_zh": item["source"],
        "summary_zh": summary,
        "keywords_zh": [],
        "relevance": 5,
    }


def build_cards(items):
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = [executor.submit(extract_page_info, item) for item in items]
        enriched = [future.result() for future in as_completed(futures)]

    enriched.sort(key=lambda item: item["updated"], reverse=True)
    cache = load_ai_cache()
    cards = []
    use_ai = ai_is_available()
    if not use_ai:
        print("AI_API_KEY not set; building untranslated fallback feed.")

    for item in enriched:
        key = cache_key(item)
        card = None
        if use_ai:
            card = cache.get(key)
            if not card:
                try:
                    card = call_ai(item)
                    cache[key] = card
                    time.sleep(0.2)
                except Exception as exc:
                    print(f"Warning: AI failed for {item['link']}: {exc}", file=sys.stderr)
                    card = fallback_card(item, reason="failed")
                if AI_DELAY_SECONDS > 0:
                    time.sleep(AI_DELAY_SECONDS)
        else:
            card = fallback_card(item)

        cards.append(
            {
                **item,
                "title_zh": normalize_space(card.get("title_zh") or item["title"]),
                "topic_zh": normalize_space(card.get("topic_zh") or item["source"]),
                "summary_zh": normalize_space(card.get("summary_zh") or item.get("raw_summary", "")),
                "keywords_zh": card.get("keywords_zh") or [],
                "relevance": int(card.get("relevance") or 5),
            }
        )

    if use_ai:
        save_ai_cache(cache)
    return sorted(cards, key=lambda item: item["updated"], reverse=True)


def item_description(item):
    keywords = "\u3001".join(str(keyword) for keyword in item.get("keywords_zh", []) if keyword)
    parts = [
        f"<p><strong>\u4e3b\u9898\uff1a</strong>{html.escape(item['topic_zh'])}</p>",
        f"<p>{html.escape(item['summary_zh'])}</p>",
        f"<p><strong>\u6765\u6e90\uff1a</strong>{html.escape(item['source'])}</p>",
        f"<p><strong>\u76f8\u5173\u5ea6\uff1a</strong>{item['relevance']}/10</p>",
    ]
    if keywords:
        parts.append(f"<p><strong>\u5173\u952e\u8bcd\uff1a</strong>{html.escape(keywords)}</p>")
    if item.get("title") and item["title"] != item["title_zh"]:
        parts.append(f"<p><strong>\u539f\u6807\u9898\uff1a</strong>{html.escape(item['title'])}</p>")
    return "\n".join(parts)


def build_feed(cards, config):
    site = config["site"]
    now = rss_date(dt.datetime.now(dt.timezone.utc))
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = site.get("title", "\u4e2d\u6587\u4fe1\u606f\u6d41")
    ET.SubElement(channel, "link").text = site.get("link", "")
    ET.SubElement(channel, "description").text = site.get("description", "\u81ea\u52a8\u7ffb\u8bd1\u548c\u6458\u8981\u540e\u7684\u4e2d\u6587 RSS\u3002")
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = now

    for item in cards:
        node = ET.SubElement(channel, "item")
        display_title = f"[{item['topic_zh']}] {item['title_zh']}"
        ET.SubElement(node, "title").text = display_title
        ET.SubElement(node, "link").text = item["link"]
        ET.SubElement(node, "guid", {"isPermaLink": "true"}).text = item["link"]
        ET.SubElement(node, "pubDate").text = rss_date(item["updated"])
        ET.SubElement(node, "description").text = item_description(item)
        ET.SubElement(node, "category").text = item["topic_zh"]

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss,
        encoding="unicode",
        short_empty_elements=False,
    )


def build_daily_markdown(cards, config):
    title = config["site"].get("title", "\u4e2d\u6587\u4fe1\u606f\u6d41")
    lines = [
        f"# {title}",
        "",
        f"- \u751f\u6210\u65f6\u95f4\uff1a{dt.datetime.now(dt.timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- \u6761\u76ee\u6570\uff1a{len(cards)}",
        "",
    ]
    grouped = {}
    for item in cards:
        grouped.setdefault(item["topic_zh"], []).append(item)
    for topic, items in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True):
        lines.extend([f"## {topic}", ""])
        for item in items:
            date_text = item["updated"].astimezone().strftime("%Y-%m-%d")
            lines.append(f"### [{item['title_zh']}]({item['link']})")
            lines.append("")
            lines.append(f"- \u6765\u6e90\uff1a{item['source']}")
            lines.append(f"- \u65e5\u671f\uff1a{date_text}")
            lines.append(f"- \u76f8\u5173\u5ea6\uff1a{item['relevance']}/10")
            if item.get("keywords_zh"):
                lines.append(f"- \u5173\u952e\u8bcd\uff1a{'\u3001'.join(item['keywords_zh'])}")
            if item.get("title") and item["title"] != item["title_zh"]:
                lines.append(f"- \u539f\u6807\u9898\uff1a{item['title']}")
            lines.append("")
            lines.append(item["summary_zh"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_index(config):
    title = html.escape(config["site"].get("title", "\u4e2d\u6587\u4fe1\u606f\u6d41"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
</head>
<body>
  <h1>{title}</h1>
  <ul>
    <li><a href="feed.xml">\u8ba2\u9605 feed.xml</a></li>
    <li><a href="daily.md">\u67e5\u770b Markdown \u65e5\u62a5</a></li>
    <li><a href="items.json">\u67e5\u770b JSON \u6570\u636e</a></li>
  </ul>
</body>
</html>
"""


def main():
    config = load_config()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    items = collect_items(config)
    cards = build_cards(items)

    (OUTPUT_DIR / "feed.xml").write_text(build_feed(cards, config), encoding="utf-8")
    (OUTPUT_DIR / "daily.md").write_text(build_daily_markdown(cards, config), encoding="utf-8")
    (OUTPUT_DIR / "items.json").write_text(
        json.dumps(
            [
                {
                    **item,
                    "updated": item["updated"].isoformat(),
                }
                for item in cards
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "index.html").write_text(build_index(config), encoding="utf-8")
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Wrote {OUTPUT_DIR / 'feed.xml'} with {len(cards)} items")


if __name__ == "__main__":
    main()
