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

    print(f"=== Battery Digest Pipeline: {args.date} ===")

    # Step 1: Scrape (if this fails, still try to build with existing content)
    scrape_ok = run_step("Step 1: Scraping sources",
        [sys.executable, "scripts/scrape.py", "--date", args.date])

    # Step 2: Curate (only if scrape succeeded)
    if scrape_ok:
        curate_cmd = [sys.executable, "scripts/curate.py", "--date", args.date, "--provider", args.provider]
        if args.model:
            curate_cmd.extend(["--model", args.model])
        run_step("Step 2: Curating digest", curate_cmd)

    # Step 3: Always build (ensures site is up-to-date even if today's curate failed)
    run_step("Step 3: Building site", [sys.executable, "build.py"])

    print(f"\nDONE: Pipeline complete for {args.date}")


if __name__ == "__main__":
    main()
