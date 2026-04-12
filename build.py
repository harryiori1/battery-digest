#!/usr/bin/env python3
"""Battery Digest - Static Site Builder

Reads markdown content from content/, renders with Jinja2 templates,
and outputs a complete static site to output/.

Usage:
    python build.py                 # build main site
    python build.py --site solidstate  # build solid-state site
"""

import argparse
import os
import re
import shutil
import glob
from datetime import datetime
from collections import defaultdict

import yaml
import markdown
from jinja2 import Environment, FileSystemLoader
from feedgen.feed import FeedGenerator


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# These are set per-site in build()
CONTENT_DIR = os.path.join(BASE_DIR, "content")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


def load_config(site="main"):
    # Try site-specific config first, fall back to config.yaml
    site_config = os.path.join(BASE_DIR, "config", "sites", f"{site}.yaml")
    if os.path.exists(site_config):
        with open(site_config, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_frontmatter(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    meta = yaml.safe_load(parts[1])
    body = parts[2].strip()
    return meta or {}, body


def md_to_html(text):
    return markdown.markdown(text, extensions=["extra"])


def split_stories(html):
    """Split rendered HTML by <h2> headings into story sections.
    Strips the leading <h2>...</h2> from each section.
    """
    parts = re.split(r"(?=<h2>)", html)
    sections = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        p = re.sub(r"^<h2>.*?</h2>\s*", "", p, count=1)
        if p.strip():
            sections.append(p.strip())
    return sections


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80].rstrip("-")


def format_date(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%B %d, %Y")


def format_month(year, month):
    months = ["", "January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{months[month]} {year}"


def load_digests(digests_dir=None):
    digests = []
    pattern = os.path.join(digests_dir or os.path.join(CONTENT_DIR, "digests"), "*.md")

    for filepath in glob.glob(pattern):
        meta, body = parse_frontmatter(filepath)
        if not meta.get("date"):
            continue

        html_body = md_to_html(body)
        story_sections = split_stories(html_body)

        slug = meta.get("slug")
        if not slug:
            stories = meta.get("stories", [])
            first_title = stories[0].get("title", "") if stories else ""
            slug = f"{meta['date']}-{slugify(first_title)}" if first_title else meta["date"]

        digests.append({
            "date": meta["date"],
            "date_display": format_date(meta["date"]),
            "slug": slug,
            "curated_from": meta.get("curated_from", 0),
            "tags": meta.get("tags", []),
            "stories": meta.get("stories", []),
            "quick_news": meta.get("quick_news", []),
            "html_sections": story_sections,
        })

    digests.sort(key=lambda d: d["date"], reverse=True)
    return digests


def load_pages():
    pages = []
    pattern = os.path.join(CONTENT_DIR, "pages", "*.md")

    for filepath in glob.glob(pattern):
        meta, body = parse_frontmatter(filepath)
        html_body = md_to_html(body)
        slug = meta.get("slug", os.path.splitext(os.path.basename(filepath))[0])
        pages.append({
            "title": meta.get("title", slug),
            "slug": slug,
            "html_body": html_body,
        })

    return pages


def group_by_month(digests):
    groups = defaultdict(list)
    for d in digests:
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        key = (dt.year, dt.month)
        groups[key].append(d)

    sorted_keys = sorted(groups.keys(), reverse=True)
    return [(format_month(y, m), groups[(y, m)]) for y, m in sorted_keys]


def generate_rss(digests, config):
    fg = FeedGenerator()
    fg.title(config["title"])
    fg.link(href=config["base_url"])
    fg.description(config["description"])
    fg.language("en")

    for digest in digests[:20]:
        fe = fg.add_entry()
        url = f"{config['base_url']}/digest/{digest['slug']}.html"
        fe.id(url)
        lead = digest["stories"][0]["title"] if digest["stories"] else "Daily Digest"
        fe.title(f"{digest['date']}: {lead}")
        fe.link(href=url)
        fe.published(datetime.strptime(digest["date"], "%Y-%m-%d").strftime("%Y-%m-%dT08:00:00+08:00"))
        summary_lines = [f"- {s['title']}" for s in digest["stories"]]
        fe.summary("\n".join(summary_lines))

    fg.rss_file(os.path.join(OUTPUT_DIR, "feed.xml"))


def build(site="main"):
    global CONTENT_DIR, OUTPUT_DIR

    if site == "main":
        CONTENT_DIR = os.path.join(BASE_DIR, "content")
        OUTPUT_DIR = os.path.join(BASE_DIR, "output")
    else:
        CONTENT_DIR = os.path.join(BASE_DIR, "content")  # pages are shared
        OUTPUT_DIR = os.path.join(BASE_DIR, f"output-{site}")

    config = load_config(site)
    year = datetime.now().year

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
    ctx = {"site": config, "year": year}

    if site == "main":
        digests_dir = os.path.join(BASE_DIR, "content", "digests")
    else:
        digests_dir = os.path.join(BASE_DIR, "content", f"digests-{site}")
        os.makedirs(digests_dir, exist_ok=True)

    digests = load_digests(digests_dir)
    pages = load_pages()

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Home page
    home_tpl = env.get_template("home.html")
    home_digests = digests[:config.get("digests_per_home_page", 15)]
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(home_tpl.render(**ctx, digests=home_digests))

    # Digest pages
    digest_tpl = env.get_template("digest.html")
    digest_out = os.path.join(OUTPUT_DIR, "digest")
    os.makedirs(digest_out, exist_ok=True)

    for i, digest in enumerate(digests):
        prev_d = digests[i + 1] if i + 1 < len(digests) else None
        next_d = digests[i - 1] if i > 0 else None
        out_path = os.path.join(digest_out, f"{digest['slug']}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(digest_tpl.render(**ctx, digest=digest, prev_digest=prev_d, next_digest=next_d))

    # Archive page
    archive_tpl = env.get_template("archive.html")
    archive_out = os.path.join(OUTPUT_DIR, "archive")
    os.makedirs(archive_out, exist_ok=True)
    archive_groups = group_by_month(digests)
    with open(os.path.join(archive_out, "index.html"), "w", encoding="utf-8") as f:
        f.write(archive_tpl.render(**ctx, archive_groups=archive_groups))

    # Static pages
    page_tpl = env.get_template("page.html")
    for page in pages:
        page_out_dir = os.path.join(OUTPUT_DIR, page["slug"])
        os.makedirs(page_out_dir, exist_ok=True)
        with open(os.path.join(page_out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(page_tpl.render(**ctx, page=page))

    # Subscribe page
    subscribe_tpl_path = os.path.join(TEMPLATES_DIR, "subscribe.html")
    if os.path.exists(subscribe_tpl_path):
        subscribe_tpl = env.get_template("subscribe.html")
        subscribe_out = os.path.join(OUTPUT_DIR, "subscribe")
        os.makedirs(subscribe_out, exist_ok=True)
        with open(os.path.join(subscribe_out, "index.html"), "w", encoding="utf-8") as f:
            f.write(subscribe_tpl.render(**ctx))

    # RSS
    if digests:
        generate_rss(digests, config)

    # Static files
    static_out = os.path.join(OUTPUT_DIR, "static")
    if os.path.exists(STATIC_DIR):
        shutil.copytree(STATIC_DIR, static_out)

    print(f"Built {len(digests)} digests, {len(pages)} pages -> {OUTPUT_DIR}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Battery Digest - Static Site Builder")
    parser.add_argument("--site", default="main", help="Site to build (main, solidstate)")
    args = parser.parse_args()
    build(site=args.site)
