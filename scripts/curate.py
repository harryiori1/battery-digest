#!/usr/bin/env python3
"""Battery Digest - LLM Curator

Reads raw scraped articles and uses LLM API to select important stories,
write analysis, and output a daily digest .md file.

Supports: Google Gemini (default), Anthropic Claude

Usage:
    python scripts/curate.py                              # curate today (Gemini)
    python scripts/curate.py --provider claude             # use Claude instead
    python scripts/curate.py --date 2026-04-02
    python scripts/curate.py --dry-run
    python scripts/curate.py --model gemini-2.5-flash      # override model

Requires GEMINI_API_KEY or ANTHROPIC_API_KEY environment variable.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "raw"
CONTENT_DIR = BASE_DIR / "content" / "digests"

logger = logging.getLogger("curate")

SYSTEM_PROMPT = """Battery news editor. Pick top 3 stories, 2 quick news.

Scoring: cross-source corroboration (strongest signal) > source authority > recency > engagement.

Output a Markdown file EXACTLY like this:

---
date: "YYYY-MM-DD"
slug: "YYYY-MM-DD-short-slug"
curated_from: NUMBER
tags: [tag1, tag2]
stories:
  - title: "Short Title"
    subtitle: "One sentence"
  - title: "Short Title"
    subtitle: "One sentence"
  - title: "Short Title"
    subtitle: "One sentence"
quick_news:
  - title: "Brief item one"
  - title: "Brief item two"
---

## 01 Short Title
Write a comprehensive 200-500 word article that covers the key details: what happened, why it matters for the battery industry, relevant numbers/specs, and broader implications. Write as a journalist summarizing the news for an English-speaking professional audience. At the end, cite sources as inline links like [Source Name](url).

## 02 Short Title
200-500 word article. [Source Name](url).

## 03 Short Title
200-500 word article. [Source Name](url).

Rules:
- ALWAYS quote titles and subtitles in YAML frontmatter with double quotes.
- Each article body MUST be 200-500 words. Readers should be able to understand the full story without clicking any links. This is especially important for Chinese-source stories where English readers cannot read the original.
- Include specific details: numbers, company names, specs, dates, market context.
- Real URLs only. Output ONLY the markdown.
- NEVER repeat topics from "ALREADY COVERED" list. If a story is an update on a previously covered topic (e.g. same company, same product, same trend), skip it.
- Titles must be SPECIFIC. Include company names, numbers, or concrete details. BAD: "Global EV Market Trends", "Advancements in Battery Technology". GOOD: "CATL Opens 100GWh Hungary Plant", "Sodium-Ion Cells Hit 200Wh/kg in Lab Tests".
- If fewer than 3 genuinely new stories exist, output only the ones that are real. NEVER pad with filler like "No New Developments"."""


def load_raw_data(date_str):
    """Load the raw scraped data for a given date (.md with YAML frontmatter, fallback to .json)."""
    md_path = DATA_DIR / f"{date_str}.md"
    json_path = DATA_DIR / f"{date_str}.json"

    if md_path.exists():
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1])
                except yaml.YAMLError as e:
                    logger.error(f"Failed to parse {md_path}: {e}")
                    sys.exit(1)

    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    logger.error(f"Raw data not found: {md_path}")
    logger.error(f"Run 'python scripts/scrape.py --date {date_str}' first.")
    sys.exit(1)


def format_articles_for_prompt(raw_data):
    """Format scraped articles into a compact list for the LLM."""
    lines = []
    for i, article in enumerate(raw_data["articles"][:25], 1):
        title = article['title']
        original = article.get('original_title')
        if original:
            lines.append(f"[{i}] [{article['source_name']}] {title} (原文: {original})")
        else:
            lines.append(f"[{i}] [{article['source_name']}] {title}")
        lines.append(f"    {article['url']}")
        snippet = article.get('snippet', '')
        if snippet:
            lines.append(f"    Summary: {snippet[:400]}")
    return "\n".join(lines)


def load_recent_story_titles(date_str, days=7):
    """Load story titles from recent digests to avoid repeating topics."""
    from datetime import datetime, timedelta
    titles = []
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    for i in range(1, days + 1):
        prev_date = d - timedelta(days=i)
        path = CONTENT_DIR / f"{prev_date}.md"
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"):
            continue
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            meta = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            continue
        if meta and meta.get("stories"):
            for s in meta["stories"]:
                if s.get("title"):
                    titles.append(f"[{prev_date}] {s['title']}")
    return titles


def build_user_prompt(raw_data):
    """Construct the user prompt with today's articles."""
    articles_text = format_articles_for_prompt(raw_data)
    total = raw_data["total_articles"]
    date_str = raw_data["date"]

    # Load recent stories to avoid repetition
    recent = load_recent_story_titles(date_str, days=7)
    avoid_section = ""
    if recent:
        avoid_list = "\n".join(f"- {t}" for t in recent)
        avoid_section = f"\n\nALREADY COVERED (do NOT repeat these topics):\n{avoid_list}\n"

    return f"""Date: {date_str}. {total} articles. Pick top 3, write digest.{avoid_section}

{articles_text}"""


# --- LLM Providers ---

def call_gemini(user_prompt, model):
    """Call Google Gemini API."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable not set.")
        logger.error("Get a key at https://aistudio.google.com/apikey")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    logger.info(f"Calling Gemini API (model: {model})...")

    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.3,
            max_output_tokens=4096,
        ),
    )

    text = response.text
    tokens_in = response.usage_metadata.prompt_token_count
    tokens_out = response.usage_metadata.candidates_token_count
    logger.info(f"Response: {len(text)} chars, usage: {tokens_in} input / {tokens_out} output tokens")
    return text


def call_claude(user_prompt, model):
    """Call Anthropic Claude API."""
    import anthropic

    client = anthropic.Anthropic()
    logger.info(f"Calling Claude API (model: {model})...")

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=0.3,
    )

    text = response.content[0].text
    logger.info(f"Response: {len(text)} chars, "
                f"usage: {response.usage.input_tokens} input / {response.usage.output_tokens} output tokens")
    return text


def call_groq(user_prompt, model):
    """Call Groq API (OpenAI-compatible)."""
    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY environment variable not set.")
        sys.exit(1)

    client = Groq(api_key=api_key)
    logger.info(f"Calling Groq API (model: {model})...")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=4096,
    )

    text = response.choices[0].message.content
    logger.info(f"Response: {len(text)} chars, "
                f"usage: {response.usage.prompt_tokens} input / {response.usage.completion_tokens} output tokens")
    return text


# --- Validation ---

def extract_markdown(response_text):
    """Extract markdown content, stripping any preamble before frontmatter.
    Auto-inserts missing closing --- if frontmatter is followed directly by ## headings.
    """
    idx = response_text.find("---")
    if idx == -1:
        logger.warning("No frontmatter delimiter found in response")
        return response_text
    text = response_text[idx:]

    # Check if closing --- is missing (frontmatter runs into ## body)
    parts = text.split("---", 2)
    if len(parts) < 3:
        # Try to find where frontmatter ends and body begins (## heading)
        body_start = text.find("\n## ", 3)
        if body_start != -1:
            frontmatter = text[3:body_start].strip()
            body = text[body_start:].strip()
            text = f"---\n{frontmatter}\n---\n\n{body}"
            logger.info("Auto-inserted missing closing --- for frontmatter")

    # Fix unquoted YAML values that contain colons (breaks YAML parsing)
    lines = text.split("\n")
    in_frontmatter = False
    for i, line in enumerate(lines):
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            for field in ("title:", "subtitle:"):
                stripped = line.lstrip()
                if stripped.startswith(f"- {field}") or stripped.startswith(field):
                    # Extract the value part after the field key
                    key_end = line.index(field) + len(field)
                    value = line[key_end:].strip()
                    # If value contains a colon and isn't already quoted, quote it
                    if ":" in value and not (value.startswith('"') or value.startswith("'")):
                        escaped = value.replace('"', '\\"')
                        lines[i] = line[:key_end] + f' "{escaped}"'
                        break
    text = "\n".join(lines)

    return text


def validate_output(md_content, raw_data):
    """Validate the generated markdown. Returns list of warnings."""
    warnings = []

    if not md_content.startswith("---"):
        warnings.append("Output does not start with ---")
        return warnings

    parts = md_content.split("---", 2)
    if len(parts) < 3:
        warnings.append("Could not find closing --- for frontmatter")
        return warnings

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        warnings.append(f"Frontmatter YAML parse error: {e}")
        return warnings

    if not meta:
        warnings.append("Frontmatter is empty")
        return warnings

    for field in ["date", "slug", "stories", "tags", "curated_from"]:
        if field not in meta:
            warnings.append(f"Missing frontmatter field: {field}")

    stories = meta.get("stories", [])
    if len(stories) < 1:
        warnings.append("No stories found in frontmatter")
    elif len(stories) != 3:
        warnings.append(f"Expected 3 stories, got {len(stories)}")

    quick_news = meta.get("quick_news", [])
    if len(quick_news) < 1:
        warnings.append("No quick_news found in frontmatter")

    # Check body has ## headings
    body = parts[2].strip() if len(parts) > 2 else ""
    h2_count = body.count("## ")
    if h2_count < 3:
        warnings.append(f"Expected 3 story headings in body, found {h2_count}")

    return warnings


# --- Main ---

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "claude": "claude-sonnet-4-20250514",
    "groq": "llama-3.3-70b-versatile",
}


def extract_key_terms(title):
    """Extract company names, numbers, and key nouns from a title (works cross-language)."""
    terms = set()
    # Extract significant numbers (4+ digits, e.g. 80000, 5000, not percentages like 42.1)
    for m in re.findall(r"\b(\d[\d,]{3,})\b", title):
        terms.add(m.replace(",", ""))
    # Known company/product names (match in any language)
    known = ["tesla", "byd", "catl", "samsung sdi", "panasonic", "nio", "xpeng",
             "rivian", "lucid", "vinfast", "sk on", "quantumscape", "solid power",
             "supercharger", "flash charging", "charging station", "battery swap",
             "lfp", "nmc", "sodium-ion", "solid-state", "market share",
             "特斯拉", "比亚迪", "宁德时代", "超充", "充电桩", "固态", "钠离子", "换电"]
    # Strip punctuation for matching
    title_clean = re.sub(r"['\"\-]", "", title.lower())
    for k in known:
        if k.replace("-", "") in title_clean:
            terms.add(k)
    return terms


def pre_filter_covered_articles(raw_data, date_str):
    """Remove articles whose topics overlap with previous digest stories."""
    from difflib import SequenceMatcher

    recent_titles = load_recent_story_titles(date_str, days=7)
    # Strip the date prefix like "[2026-04-03] "
    recent_titles = [t.split("] ", 1)[-1] for t in recent_titles]

    if not recent_titles:
        return raw_data

    # Pre-compute key terms for each previous digest story
    prev_term_sets = [(t, extract_key_terms(t)) for t in recent_titles]

    filtered = []
    for a in raw_data["articles"]:
        title = a["title"]
        title_lower = title.lower()
        is_covered = False

        # Check 1: text similarity (same language)
        for prev, _ in prev_term_sets:
            ratio = SequenceMatcher(None, title_lower, prev.lower()).ratio()
            if ratio >= 0.50:
                logger.debug(f"  Pre-filtered (text sim={ratio:.2f}): '{title}' ~ '{prev}'")
                is_covered = True
                break

        # Check 2: key term overlap (cross-language)
        if not is_covered:
            article_terms = extract_key_terms(title)
            if article_terms:
                for prev, prev_terms in prev_term_sets:
                    if prev_terms:
                        overlap = article_terms & prev_terms
                        # 1 match is enough if it's a specific number (4+ digits)
                        has_specific_number = any(len(t) >= 4 and t.isdigit() for t in overlap)
                        if len(overlap) >= 2 or has_specific_number:
                            logger.debug(f"  Pre-filtered (terms {overlap}): '{title}' ~ '{prev}'")
                            is_covered = True
                            break

        if not is_covered:
            filtered.append(a)

    removed = len(raw_data["articles"]) - len(filtered)
    if removed:
        logger.info(f"Pre-filter: removed {removed} articles already covered in recent digests, {len(filtered)} remain")

    raw_data = dict(raw_data)
    raw_data["articles"] = filtered
    raw_data["total_articles"] = len(filtered)
    return raw_data


def curate(date_str, provider, model, dry_run=False):
    """Run the curation pipeline."""
    raw_data = load_raw_data(date_str)

    if raw_data["total_articles"] == 0:
        logger.error("No articles found in raw data. Nothing to curate.")
        sys.exit(1)

    # Remove articles already covered in recent digests
    raw_data = pre_filter_covered_articles(raw_data, date_str)

    if raw_data["total_articles"] == 0:
        logger.info("All articles already covered in recent digests. Skipping digest for today.")
        print(f"No new stories for {date_str} - all articles already covered recently.")
        return

    logger.info(f"Curating {raw_data['total_articles']} articles for {date_str} (provider: {provider})")

    user_prompt = build_user_prompt(raw_data)
    logger.debug(f"User prompt length: {len(user_prompt)} chars")

    if provider == "gemini":
        response_text = call_gemini(user_prompt, model)
    elif provider == "claude":
        response_text = call_claude(user_prompt, model)
    elif provider == "groq":
        response_text = call_groq(user_prompt, model)
    else:
        logger.error(f"Unknown provider: {provider}")
        sys.exit(1)

    md_content = extract_markdown(response_text)
    warnings = validate_output(md_content, raw_data)

    for w in warnings:
        logger.warning(f"Validation: {w}")

    if dry_run:
        print(md_content)
        print(f"\n--- Validation: {len(warnings)} warning(s) ---")
        for w in warnings:
            print(f"  ⚠ {w}")
        return

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CONTENT_DIR / f"{date_str}.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"Curated digest -> {output_path}")
    if warnings:
        print(f"  WARNING: {len(warnings)} validation warning(s), check logs")


def main():
    parser = argparse.ArgumentParser(description="Battery Digest - LLM Curator")
    parser.add_argument("--date", default=str(date.today()), help="Date to curate (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print output to stdout, don't write file")
    parser.add_argument("--provider", default="groq", choices=["gemini", "claude", "groq"],
                        help="LLM provider (default: gemini)")
    parser.add_argument("--model", default=None, help="Model name (defaults per provider)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    model = args.model or DEFAULT_MODELS[args.provider]
    curate(args.date, args.provider, model, args.dry_run)


if __name__ == "__main__":
    main()
