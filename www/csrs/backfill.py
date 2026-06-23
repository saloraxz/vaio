#!/usr/bin/env python3
"""
CSRS Historical Backfill
========================
Walks HLTV results from START_DATE to today in 2-day windows.

Features:
  - Checkpoint file (backfill_progress.json) tracks every completed window
    so you can stop/restart at any time without re-scraping old dates
  - URL-level dedup against existing history (same logic as run_auto_import)
  - Git push after each successful window so data.save stays current
  - Failed URLs logged to backfill_failed.log for manual retry

Usage (run inside the csrs-daemon container or directly on server):
  python3 backfill.py
  python3 backfill.py --start 2026-01-01   # override start date
  python3 backfill.py --dry-run            # list windows only, no import
  python3 backfill.py --retry-failed       # only retry URLs in backfill_failed.log
"""

import json
import os
import sys
import argparse
from datetime import datetime, timedelta, date

# ── must be run from the repo root where CSRS.py lives ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import CSRS  # noqa: E402  (loads data globals, defines all functions)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
START_DATE      = date(2026, 1, 1)   # first day to import
WINDOW_DAYS     = 2                  # days per scrape window
DATA_DIR        = os.environ.get("CSRS_DATA_DIR", ".")
CHECKPOINT_FILE = os.path.join(DATA_DIR, "backfill_progress.json")
FAILED_LOG      = os.path.join(DATA_DIR, "backfill_failed.log")


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------
def load_checkpoint() -> dict:
    """Returns {completed_windows: [...], last_run: '...'}"""
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed_windows": [], "last_run": None}


def save_checkpoint(state: dict) -> None:
    state["last_run"] = datetime.now().isoformat()
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_failed(urls: list, window: str) -> None:
    with open(FAILED_LOG, "a") as f:
        for u in urls:
            f.write(f"{window}\t{u}\n")


def load_failed_urls() -> list:
    if not os.path.exists(FAILED_LOG):
        return []
    urls = []
    with open(FAILED_LOG) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) == 2:
                urls.append(parts[1])
    return urls


# ---------------------------------------------------------------------------
# Build window list
# ---------------------------------------------------------------------------
def build_windows(start: date, end: date, window_days: int):
    """Yields (start_str, end_str) tuples covering start..end inclusive."""
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=window_days - 1), end)
        yield cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")
        cur += timedelta(days=window_days)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_backfill(start_date: date, dry_run: bool = False) -> None:
    today = date.today()
    state = load_checkpoint()
    completed = set(state["completed_windows"])

    windows = list(build_windows(start_date, today, WINDOW_DAYS))
    pending = [w for w in windows if f"{w[0]}/{w[1]}" not in completed]

    print(f"\n{'='*60}")
    print(f"CSRS Backfill — {start_date} → {today}")
    print(f"  Total windows : {len(windows)}")
    print(f"  Already done  : {len(windows) - len(pending)}")
    print(f"  To process    : {len(pending)}")
    print(f"{'='*60}\n")

    if dry_run:
        for s, e in pending:
            print(f"  Would import: {s} → {e}")
        return

    if not pending:
        print("Nothing to do — all windows already completed.")
        return

    # Pre-load current imported URL set once (faster than re-checking each window)
    CSRS.load_all()
    already_imported = CSRS.get_imported_urls(CSRS.history)
    print(f"Loaded {len(CSRS.history)} existing matches, "
          f"{len(already_imported)} unique URLs already in history.\n")

    total_imported = 0
    total_failed   = 0

    for i, (win_start, win_end) in enumerate(pending, 1):
        window_key = f"{win_start}/{win_end}"
        print(f"[{i}/{len(pending)}] Window {win_start} → {win_end}")

        # ── Scrape phase ──
        try:
            with CSRS.BrowserSession() as sess:
                scraped_urls = CSRS.scrape_hltv_results(
                    win_start, win_end, context=sess.context
                )
        except Exception as e:
            print(f"  [ERROR] Scrape failed: {e} — skipping window, will retry next run")
            continue

        # ── Filter already-imported ──
        # Refresh after each window since we're adding to history as we go
        already_imported = CSRS.get_imported_urls(CSRS.history)
        new_urls = [u for u in scraped_urls if u not in already_imported]

        print(f"  Scraped {len(scraped_urls)}, "
              f"{len(scraped_urls) - len(new_urls)} already imported, "
              f"{len(new_urls)} new")

        if not new_urls:
            # Mark as done even if nothing new — the window is covered
            completed.add(window_key)
            state["completed_windows"] = sorted(completed)
            save_checkpoint(state)
            print(f"  ✓ Nothing new — marked complete\n")
            continue

        # ── Import phase ──
        try:
            with CSRS.BrowserSession() as sess:
                count, failed = CSRS._import_url_list(new_urls, sess.context)
        except Exception as e:
            print(f"  [ERROR] Import phase failed: {e} — will retry next run")
            continue

        # ── Save after every window ──
        CSRS._vrs_session_cache.clear()
        CSRS.save_all(silent=True)

        total_imported += count
        total_failed   += len(failed)

        if failed:
            print(f"  [WARN] {len(failed)} failed:")
            for u in failed:
                print(f"    FAILED: {u}")
            log_failed(failed, window_key)

        # ── Mark window complete and checkpoint ──
        completed.add(window_key)
        state["completed_windows"] = sorted(completed)
        save_checkpoint(state)

        # ── Git push after each window so data.save stays live ──
        CSRS._git_push()

        print(f"  ✓ Imported {count}, failed {len(failed)} — checkpoint saved\n")

    print(f"\n{'='*60}")
    print(f"Backfill complete.")
    print(f"  Total imported : {total_imported}")
    print(f"  Total failed   : {total_failed}")
    if total_failed:
        print(f"  Failed URLs logged to: {FAILED_LOG}")
        print(f"  Re-run with --retry-failed to attempt them again")
    print(f"{'='*60}\n")


def run_retry_failed() -> None:
    """Retry just the URLs that failed in a previous backfill run."""
    urls = load_failed_urls()
    if not urls:
        print("No failed URLs in backfill_failed.log")
        return

    print(f"Retrying {len(urls)} previously failed URLs...")
    CSRS.load_all()

    try:
        with CSRS.BrowserSession() as sess:
            count, still_failed = CSRS._import_url_list(urls, sess.context)
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    CSRS._vrs_session_cache.clear()
    CSRS.save_all(silent=True)
    CSRS._git_push()

    print(f"Retry complete: {count} imported, {len(still_failed)} still failing")
    if still_failed:
        # Overwrite failed log with only those still failing
        with open(FAILED_LOG, "w") as f:
            for u in still_failed:
                f.write(f"retry\t{u}\n")
    else:
        os.remove(FAILED_LOG)
        print(f"All retries succeeded — {FAILED_LOG} removed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CSRS historical backfill")
    parser.add_argument(
        "--start", default=START_DATE.isoformat(), metavar="YYYY-MM-DD",
        help=f"Start date (default: {START_DATE})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print windows that would be processed, then exit"
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Retry URLs logged in backfill_failed.log"
    )
    args = parser.parse_args()

    if args.retry_failed:
        run_retry_failed()
    else:
        start = date.fromisoformat(args.start)
        run_backfill(start, dry_run=args.dry_run)
