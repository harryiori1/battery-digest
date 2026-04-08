#!/usr/bin/env python3
"""Battery Digest - News Scraper

Fetches battery-related articles from configured sources (RSS/HTML),
filters by keywords, deduplicates, translates Chinese titles to English,
and outputs a .md file for curation.

Usage:
    python scripts/scrape.py                    # scrape for today
    python scripts/scrape.py --date 2026-04-02  # specific date
    python scripts/scrape.py --source Electrek  # single source (debug)
    python scripts/scrape.py --verbose          # debug logging
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "sources.yaml"
DATA_DIR = BASE_DIR / "data" / "raw"

logger = logging.getLogger("scrape")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_client(http_config):
    return httpx.Client(
        timeout=http_config.get("timeout", 15),
        headers={"User-Agent": http_config.get("user_agent", "BatteryDigest/1.0")},
        follow_redirects=True,
    )


def strip_html(text):
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_rss(source, client):
    """Fetch and parse an RSS feed. Returns list of article dicts."""
    logger.info(f"Fetching RSS: {source['name']} ({source['url']})")
    resp = client.get(source["url"])
    resp.raise_for_status()

    feed = feedparser.parse(resp.content.decode("utf-8", errors="replace"))
    if feed.bozo:
        logger.warning(f"RSS parse warning for {source['name']}: {feed.bozo_exception}")

    articles = []
    for entry in feed.entries:
        articles.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", "").strip(),
            "source_name": source["name"],
            "date": entry.get("published", ""),
            "language": source["language"],
        })

    logger.info(f"  {source['name']}: fetched {len(articles)} articles from RSS")
    return articles


def fetch_html(source, client):
    """Scrape articles from an HTML page using CSS selectors."""
    logger.info(f"Fetching HTML: {source['name']} ({source['url']})")
    resp = client.get(source["url"])
    resp.raise_for_status()

    html_text = resp.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "lxml")
    sel = source["selectors"]

    articles = []
    items = soup.select(sel["article_list"])
    logger.debug(f"  {source['name']}: found {len(items)} items with selector '{sel['article_list']}'")

    for item in items:
        if sel.get("title"):
            title_el = item.select_one(sel["title"])
            link_el = item.select_one(sel["link"]) if sel.get("link") else title_el
        else:
            title_el = item
            link_el = item
        if not title_el:
            continue

        title_text = title_el.get_text(strip=True)
        href = link_el.get("href", "") if link_el else ""
        if not href or not title_text or len(title_text) < 8:
            continue

        articles.append({
            "title": title_text,
            "url": urljoin(source["url"], href),
            "source_name": source["name"],
            "date": str(date.today()),
            "language": source["language"],
        })

    logger.info(f"  {source['name']}: scraped {len(articles)} articles from HTML")
    return articles


def matches_keywords(article, keywords):
    """Check if article title contains battery-related keywords."""
    text = article["title"].lower()
    lang = article.get("language", "en")

    for kw in keywords.get(lang, []):
        if kw.lower() in text:
            return True

    other_lang = "zh" if lang == "en" else "en"
    for kw in keywords.get(other_lang, []):
        if kw.lower() in text:
            return True

    return False


def normalize_title(title):
    """Normalize title for comparison."""
    title = title.lower()
    title = re.sub(r"^(breaking|exclusive|update|report|opinion)\s*[:\-|]?\s*", "", title)
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def title_similarity(t1, t2):
    """Calculate similarity between two titles."""
    from difflib import SequenceMatcher

    n1 = normalize_title(t1)
    n2 = normalize_title(t2)

    ratio = SequenceMatcher(None, n1, n2).ratio()
    if ratio > 0.6:
        return ratio

    words1 = set(n1.split())
    words2 = set(n2.split())
    stopwords = {"the", "a", "an", "is", "in", "on", "at", "to", "for", "of", "and", "or",
                 "with", "its", "has", "will", "new", "says", "said", "from", "by", "as",
                 "的", "了", "在", "是", "和", "为", "与", "将"}
    words1 -= stopwords
    words2 -= stopwords
    if not words1 or not words2:
        return ratio

    overlap = words1 & words2
    jaccard = len(overlap) / len(words1 | words2)
    return max(ratio, jaccard)


def deduplicate(articles):
    """Remove duplicate articles by URL and fuzzy title similarity."""
    seen_urls = set()
    url_deduped = []
    for a in articles:
        normalized = a["url"].rstrip("/").lower().split("?")[0]
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            url_deduped.append(a)

    result = []
    for a in url_deduped:
        is_dup = False
        for kept in result:
            sim = title_similarity(a["title"], kept["title"])
            if sim >= 0.55:
                logger.debug(f"  Similar ({sim:.2f}): '{a['title']}' ~ '{kept['title']}'")
                is_dup = True
                break
        if not is_dup:
            result.append(a)

    if len(url_deduped) != len(result):
        logger.info(f"  Fuzzy dedup removed {len(url_deduped) - len(result)} similar articles")

    return result


def load_raw_md(path):
    """Parse a raw data .md file (YAML frontmatter). Returns dict or None."""
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        return yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None


def load_previous_titles(date_str, days=3):
    """Load article titles from previous days to detect cross-day reposts."""
    from datetime import timedelta
    prev_titles = []
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    for i in range(1, days + 1):
        prev_date = d - timedelta(days=i)
        # Try .md first, fall back to .json for old data
        data = load_raw_md(DATA_DIR / f"{prev_date}.md")
        if data is None:
            json_path = DATA_DIR / f"{prev_date}.json"
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        if data:
            for a in data.get("articles", []):
                prev_titles.append(a["title"])
    return prev_titles


def cross_day_dedup(articles, prev_titles):
    """Remove articles that are too similar to previous days' articles."""
    if not prev_titles:
        return articles

    result = []
    removed = 0
    for a in articles:
        is_repost = False
        for prev_t in prev_titles:
            sim = title_similarity(a["title"], prev_t)
            if sim >= 0.90:
                logger.debug(f"  Cross-day repost ({sim:.2f}): '{a['title']}' ~ '{prev_t}'")
                is_repost = True
                break
        if not is_repost:
            result.append(a)
        else:
            removed += 1

    if removed:
        logger.info(f"  Cross-day dedup removed {removed} reposted articles (checked {len(prev_titles)} previous titles)")

    return result


def translate_chinese_titles(articles):
    """Translate Chinese article titles to English using Groq API.
    Sets original_title to Chinese, replaces title with English.
    Falls back gracefully if translation fails.
    """
    zh_articles = [a for a in articles if a.get("language") == "zh"]
    if not zh_articles:
        for a in articles:
            a["original_title"] = None
        return articles

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY not set, skipping Chinese title translation")
        for a in articles:
            a["original_title"] = None
        return articles

    # Build translation prompt
    titles_list = "\n".join(f"{i+1}. {a['title']}" for i, a in enumerate(zh_articles))
    prompt = f"Translate these Chinese article titles to English. Return ONLY a JSON array of translated strings in the same order. No explanations.\n\n{titles_list}"

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        logger.info(f"Translating {len(zh_articles)} Chinese titles...")
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2048,
        )
        text = response.choices[0].message.content.strip()
        # Extract JSON array from response
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            translations = json.loads(text[start:end])
        else:
            raise ValueError(f"No JSON array found in response: {text[:200]}")

        if len(translations) != len(zh_articles):
            raise ValueError(f"Expected {len(zh_articles)} translations, got {len(translations)}")

        # Apply translations
        translation_map = {}
        for a, trans in zip(zh_articles, translations):
            translation_map[id(a)] = trans

        for a in articles:
            if id(a) in translation_map:
                a["original_title"] = a["title"]
                a["title"] = translation_map[id(a)]
            else:
                a["original_title"] = None

        logger.info(f"  Translated {len(translations)} titles")

    except Exception as e:
        logger.warning(f"Translation failed ({e}), keeping original Chinese titles")
        for a in articles:
            a["original_title"] = None

    return articles


def make_source_client(source, http_config):
    """Create a per-source client, handling ssl_verify flag."""
    verify = source.get("ssl_verify", True)
    return httpx.Client(
        timeout=http_config.get("timeout", 15),
        headers={"User-Agent": http_config.get("user_agent", "BatteryDigest/1.0")},
        follow_redirects=True,
        verify=verify,
    )


def fetch_source(source, client, http_config, max_retries):
    """Fetch a single source with retry logic."""
    needs_custom_client = source.get("ssl_verify", True) is False
    src_client = make_source_client(source, http_config) if needs_custom_client else client

    try:
        for attempt in range(max_retries + 1):
            try:
                if source["type"] == "rss":
                    return fetch_rss(source, src_client)
                elif source["type"] == "html":
                    return fetch_html(source, src_client)
                else:
                    logger.warning(f"Unknown source type: {source['type']}")
                    return []
            except Exception as e:
                if attempt < max_retries:
                    wait = (attempt + 1) * 2
                    logger.warning(f"  {source['name']} attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    logger.error(f"  {source['name']} failed after {max_retries + 1} attempts: {e}")
                    return None
    finally:
        if needs_custom_client:
            src_client.close()


def write_raw_md(output_path, output):
    """Write scraped data as .md with YAML frontmatter."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("---\n")
        yaml.dump(output, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        f.write("---\n")


def main():
    parser = argparse.ArgumentParser(description="Battery Digest - News Scraper")
    parser.add_argument("--date", default=str(date.today()), help="Date for output file (YYYY-MM-DD)")
    parser.add_argument("--source", help="Scrape a single source by name (for debugging)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    config = load_config()
    http_config = config.get("http", {})
    keywords = config.get("keywords", {})
    max_retries = http_config.get("max_retries", 2)

    # Filter sources
    sources = [s for s in config["sources"] if s.get("enabled", True)]
    if args.source:
        sources = [s for s in sources if s["name"] == args.source]
        if not sources:
            logger.error(f"Source '{args.source}' not found or disabled")
            sys.exit(1)

    logger.info(f"Scraping {len(sources)} sources for {args.date}")

    # Fetch all sources
    all_articles = []
    failed_sources = []
    client = make_client(http_config)

    for source in sources:
        result = fetch_source(source, client, http_config, max_retries)
        if result is None:
            failed_sources.append(source["name"])
        elif result:
            all_articles.extend(result)

    client.close()

    # Deduplicate (same-day: URL + fuzzy title)
    all_articles = deduplicate(all_articles)
    total_before_filter = len(all_articles)

    # Keyword filter
    filtered = [a for a in all_articles if matches_keywords(a, keywords)]
    logger.info(f"Total: {total_before_filter} articles, {len(filtered)} after keyword filter")

    # Cross-day dedup
    prev_titles = load_previous_titles(args.date, days=3)
    filtered = cross_day_dedup(filtered, prev_titles)
    if failed_sources:
        logger.warning(f"Failed sources: {', '.join(failed_sources)}")

    # Translate Chinese titles to English
    filtered = translate_chinese_titles(filtered)

    # Write output as .md
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / f"{args.date}.md"

    output = {
        "date": args.date,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "sources_attempted": len(sources),
        "sources_succeeded": len(sources) - len(failed_sources),
        "sources_failed": failed_sources,
        "total_before_filter": total_before_filter,
        "total_articles": len(filtered),
        "articles": filtered,
    }

    write_raw_md(output_path, output)

    logger.info(f"Output: {output_path} ({len(filtered)} articles)")
    print(f"Scraped {len(filtered)} battery-related articles from {len(sources) - len(failed_sources)}/{len(sources)} sources -> {output_path}")


if __name__ == "__main__":
    main()
