#!/usr/bin/env python3
"""Battery Digest - Daily Pipeline Runner

Runs scrape -> curate -> build sequentially.

Usage:
    python scripts/run_daily.py                    # run for today
    python scripts/run_daily.py --date 2026-04-02  # specific date
"""

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE_DIR = str(Path(__file__).resolve().parent.parent)


def run_step(name, cmd):
    """Run a pipeline step, return True if successful."""
    print(f"\n--- {name} ---")
    r = subprocess.run(cmd, cwd=BASE_DIR)
    if r.returncode != 0:
        print(f"  WARNING: {name} failed (exit code {r.returncode}), continuing...")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Battery Digest - Daily Pipeline")
    parser.add_argument("--date", default=str(date.today()), help="Date (YYYY-MM-DD)")
    parser.add_argument("--provider", default="groq", choices=["gemini", "claude", "groq"], help="LLM provider")
    parser.add_argument("--model", default=None, help="Model override")
    args = parser.parse_args()

    sites = ["main", "solidstate"]
    print(f"=== Battery Digest Pipeline: {args.date} ===")

    for site in sites:
        site_label = site.upper()
        print(f"\n{'='*50}")
        print(f"  Building site: {site_label}")
        print(f"{'='*50}")

        # Step 1: Scrape
        scrape_ok = run_step(f"[{site_label}] Scraping sources",
            [sys.executable, "scripts/scrape.py", "--date", args.date, "--site", site])

        # Step 2: Curate (only if scrape succeeded)
        if scrape_ok:
            curate_cmd = [sys.executable, "scripts/curate.py", "--date", args.date,
                          "--provider", args.provider, "--site", site]
            if args.model:
                curate_cmd.extend(["--model", args.model])
            run_step(f"[{site_label}] Curating digest", curate_cmd)

        # Step 3: Always build
        run_step(f"[{site_label}] Building site",
            [sys.executable, "build.py", "--site", site])

    print(f"\nDONE: Pipeline complete for {args.date} (all sites)")


if __name__ == "__main__":
    main()
