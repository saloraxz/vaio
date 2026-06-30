#!/usr/bin/env python3
"""
CSRS Historical Backfill — Results-Page-Offset
==========================================================
Walks the plain /results feed (reverse-chronological, paged via &offset=N)
instead of per-event /results?event=<id>. /results only ever lists
completed matches, so there's no future/unplayed-match filtering needed —
that risk was specific to the old per-event approach on ongoing events.

Each .result-con block on the page carries its own
data-zonedgrouping-entry-unix timestamp, which we use directly as the
stop condition: once a page's matches fall before START_DATE, we stop
paginating (the feed is newest-first, so everything after is older too).

Usage:
  python backfill.py --test-results-page                 # diagnostic only, no import
  python backfill.py --test-results-page --start 2026-01-01 --end 2026-06-29
  python backfill.py --import --start 2026-01-01          # real backfill, imports matches
  python backfill.py --retry-failed                       # retry URLs in backfill_failed.log
"""

import json, os, sys, argparse, time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import CSRS

START_DATE       = date(2026, 1, 1)
DATA_DIR         = os.environ.get("CSRS_DATA_DIR", ".")
CHECKPOINT_FILE  = os.path.join(DATA_DIR, "data", "backfill_progress.json")
FAILED_LOG       = os.path.join(DATA_DIR, "logs", "fails", "backfill_failed.log")

# How long to wait between retry attempts when a batch has failures
RETRY_DELAY_SECONDS   = 30
# Maximum number of retry passes before giving up on a set of URLs
MAX_RETRY_PASSES      = 5


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed_event_ids": [], "last_run": None}


def save_checkpoint(state):
    state["last_run"] = datetime.now().isoformat()
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_failed(urls, event_id):
    with open(FAILED_LOG, "a") as f:
        for u in urls:
            f.write(f"{event_id}\t{u}\n")


def clear_failed_for_event(event_id):
    """Remove all failed-log entries for a given event (called when retries succeed)."""
    if not os.path.exists(FAILED_LOG):
        return
    lines = []
    with open(FAILED_LOG) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) == 2:
                try:
                    if int(parts[0]) != event_id:
                        lines.append(line)
                except ValueError:
                    lines.append(line)
    with open(FAILED_LOG, "w") as f:
        f.writelines(lines)


def load_failed_urls():
    if not os.path.exists(FAILED_LOG):
        return []
    results = []
    with open(FAILED_LOG) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) == 2:
                try:
                    results.append((int(parts[0]), parts[1]))
                except ValueError:
                    pass
    return results


# ---------------------------------------------------------------------------
# Retry loop — keeps retrying a URL list until all succeed or max passes hit
# ---------------------------------------------------------------------------

def _import_with_retry(
    urls: list[str],
    sess_context,
    event_id: int,
    max_passes: int = MAX_RETRY_PASSES,
    retry_delay: int = RETRY_DELAY_SECONDS,
) -> tuple[int, list[str]]:
    """
    Wraps CSRS._import_url_list with an automatic retry loop.

    Keeps retrying failed URLs until either:
      - all URLs succeed, or
      - max_passes retry attempts are exhausted.

    Returns (total_imported, still_failed_urls).
    """
    remaining = list(urls)
    total_imported = 0
    pass_num = 0

    while remaining and pass_num < max_passes:
        pass_num += 1
        if pass_num > 1:
            print(f"    [RETRY {pass_num}/{max_passes}] {len(remaining)} URL(s) — waiting {retry_delay}s...")
            time.sleep(retry_delay)

        count, failed = CSRS._import_url_list(remaining, sess_context)
        total_imported += count

        if not failed:
            print(f"    All {len(remaining)} URL(s) imported successfully (pass {pass_num}).")
            remaining = []
            break

        print(f"    Pass {pass_num}: {count} imported, {len(failed)} still failed.")
        remaining = failed

    return total_imported, remaining


# ---------------------------------------------------------------------------
# Results-page parsing — group matches by day, stop at START_DATE boundary
# ---------------------------------------------------------------------------

def _parse_results_page(html: str) -> list[dict]:
    """
    Parses one /results page into a list of day-groups, preserving the
    page's natural (newest-first) order:

      [{"headline": "Results for June 23rd 2026",
        "matches": [{"url": "...", "unix_ms": 1782164735000}, ...]},
       ...]

    Each match's unix_ms comes straight from data-zonedgrouping-entry-unix
    on its .result-con block — no date-string parsing needed.
    """
    import re
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results_container = soup.find("div", class_="results")
    search_root = results_container if results_container is not None else soup

    groups = []
    for sublist in search_root.find_all("div", class_="results-sublist"):
        headline_div = sublist.find("div", class_="standard-headline")
        headline = headline_div.get_text(strip=True) if headline_div else None

        matches = []
        seen: set[str] = set()
        for con in sublist.find_all("div", class_="result-con"):
            a = con.find("a", href=re.compile(r'^/matches/\d+/'))
            if not a:
                continue
            full_url = "https://www.hltv.org" + a["href"]
            if full_url in seen:
                continue
            seen.add(full_url)

            unix_ms_raw = con.get("data-zonedgrouping-entry-unix")
            try:
                unix_ms = int(unix_ms_raw) if unix_ms_raw else None
            except ValueError:
                unix_ms = None

            matches.append({"url": full_url, "unix_ms": unix_ms})

        if matches:
            groups.append({"headline": headline, "matches": matches})

    return groups


def run_backfill(start_date: date, dry_run: bool = False, max_pages: int = 1000):
    """
    Real offset-based backfill: walks the plain /results feed page by page
    (offset 0, 100, 200, ...), collecting match URLs whose timestamp is
    >= start_date. Stops as soon as a page's matches drop below start_date,
    since /results is strictly newest-first.
    """
    import random as _random

    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    start_unix_ms = int(start_dt.timestamp() * 1000)

    cookies = CSRS._load_hltv_cookies()
    pw_cookies = [
        {"name": c.get("name", ""), "value": c.get("value", ""),
         "domain": c.get("domain", ".hltv.org"), "path": c.get("path", "/")}
        for c in cookies if c.get("name") and c.get("value")
    ]

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        print("ERROR: Camoufox not installed. Run: pip install camoufox && python -m camoufox fetch")
        return

    print(f"\n{'='*60}")
    print(f"CSRS Backfill — /results offset walk from {start_date} to today")
    print(f"{'='*60}\n")

    collected_urls: list[str] = []
    seen_urls: set[str] = set()
    hit_boundary = False

    final_offset = 0
    final_page_num = 0

    with Camoufox(headless=CSRS._CAMOUFOX_HEADLESS, os=("windows", "macos", "linux")) as browser:
        context = browser.new_context()
        if pw_cookies:
            context.add_cookies(pw_cookies)
        page = context.new_page()

        try:
            page.goto("https://www.hltv.org", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            print("Session warmed up.\n")
        except Exception as e:
            print(f"Warm-up warning: {e}")

        for page_num in range(max_pages):
            offset = page_num * CSRS.HLTV_RESULTS_PAGE_SIZE
            final_offset = offset
            final_page_num = page_num + 1
            url = "https://www.hltv.org/results" if offset == 0 else f"https://www.hltv.org/results?offset={offset}"
            print(f"Page {page_num + 1} (offset={offset}): GET {url}")

            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"  [FAIL] Navigation error: {e} — stopping pagination\n")
                break

            html = page.content()
            if "Just a moment" in html or "challenge-platform" in html:
                print(f"  [BLOCKED] Cloudflare challenge — stopping pagination\n")
                break

            groups = _parse_results_page(html)
            if not groups:
                print(f"  No day-groups found — assuming end of feed\n")
                break

            page_new = 0
            for grp in groups:
                # If every match in this group is older than start_date,
                # we've crossed the boundary — stop entirely (feed is
                # newest-first, so nothing after this is in range either).
                group_unix = [m["unix_ms"] for m in grp["matches"] if m["unix_ms"] is not None]
                if group_unix and max(group_unix) < start_unix_ms:
                    print(f"  Boundary hit at '{grp['headline']}' — older than {start_date}, stopping.")
                    hit_boundary = True
                    break

                for m in grp["matches"]:
                    if m["unix_ms"] is not None and m["unix_ms"] < start_unix_ms:
                        continue  # this individual match predates the boundary
                    if m["url"] not in seen_urls:
                        seen_urls.add(m["url"])
                        collected_urls.append(m["url"])
                        page_new += 1

            print(f"  +{page_new} match URL(s) in range (running total: {len(collected_urls)})\n")

            if hit_boundary:
                break

            time.sleep(_random.uniform(1.0, 2.5))

        page.close()

    # /results is newest-first; reverse so matches are imported oldest -> newest,
    # which matters because pts_before/pts_after are scraped relative to each
    # team's rating at the time of that match's import.
    collected_urls.reverse()

    print(f"{'='*60}")
    print(f"Stopped at page {final_page_num} (offset={final_offset}), boundary_hit={hit_boundary}")
    print(f"Collected {len(collected_urls)} match URL(s) from {start_date} to today (oldest -> newest order).")
    print(f"{'='*60}\n")

    if dry_run:
        print("DRY RUN — not importing. First 10 URLs (oldest first):")
        for u in collected_urls[:10]:
            print(f"  {u}")
        return

    if not collected_urls:
        print("Nothing to import.")
        return

    CSRS.load_all()
    already_imported = CSRS.get_imported_urls(CSRS.history)
    new_urls = [u for u in collected_urls if u not in already_imported]
    print(f"Loaded {len(CSRS.history)} existing matches, {len(already_imported)} unique URLs.")
    print(f"{len(collected_urls) - len(new_urls)} already imported, {len(new_urls)} new to import.\n")

    if not new_urls:
        print("Nothing new to do.")
        return

    try:
        with CSRS.BrowserSession() as sess:
            count, still_failed = _import_with_retry(new_urls, sess.context, event_id=0)
    except Exception as e:
        print(f"[ERROR] Import session failed: {e}")
        log_failed(new_urls, event_id=0)
        return

    CSRS._vrs_session_cache.clear()
    CSRS.save_all(silent=True)

    print(f"\nImported {count} match(es). {len(still_failed)} still failed.")
    if still_failed:
        log_failed(still_failed, event_id=0)
        print(f"Logged to {FAILED_LOG} — re-run with --retry-failed")

    CSRS._git_push()

    # Resimulation is unconditional — always rebuild ratings from scratch
    print("\nResimulating all ratings in chronological order...")
    CSRS.resimulate()
    CSRS.save_all(silent=True)
    CSRS._git_push()
    print("Resimulation complete.\n")


def run_retry_failed():
    entries = load_failed_urls()
    if not entries:
        print("No failed URLs in backfill_failed.log")
        return

    urls = [u for _, u in entries]
    print(f"Retrying {len(urls)} previously failed URL(s)...")
    CSRS.load_all()

    try:
        with CSRS.BrowserSession() as sess:
            count, still_failed = _import_with_retry(urls, sess.context, event_id=0)
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    CSRS._vrs_session_cache.clear()
    CSRS.save_all(silent=True)
    CSRS._git_push()

    print(f"Retry complete: {count} imported, {len(still_failed)} still failing")
    if still_failed:
        with open(FAILED_LOG, "w") as f:
            for u in still_failed:
                f.write(f"retry\t{u}\n")
    elif os.path.exists(FAILED_LOG):
        os.remove(FAILED_LOG)
        print("All retries succeeded — backfill_failed.log removed")

    # Resimulation is unconditional
    print("\nResimulating all ratings in chronological order...")
    CSRS.resimulate()
    CSRS.save_all(silent=True)
    CSRS._git_push()
    print("Resimulation complete.\n")


# ---------------------------------------------------------------------------
# TEST MODE — check whether the date-filtered /results page works here
# ---------------------------------------------------------------------------

def _test_one_results_variant(page, label: str, base_url: str, max_pages: int) -> int:
    """
    Shared core for run_test_results_page: hits base_url (+ &offset=N for
    subsequent pages) for up to max_pages pages and reports what comes back.
    Returns the total number of match URLs seen across all pages.
    """
    import re
    import time as _time, random as _random
    from bs4 import BeautifulSoup

    print(f"\n{'-'*60}")
    print(f"VARIANT: {label}")
    print(f"{'-'*60}")

    total_urls_seen = 0

    for page_num in range(max_pages):
        offset = page_num * CSRS.HLTV_RESULTS_PAGE_SIZE
        sep = "&" if "?" in base_url else "?"
        url = base_url if offset == 0 else f"{base_url}{sep}offset={offset}"
        print(f"--- Page {page_num + 1} (offset={offset}) ---")
        print(f"GET {url}")

        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  [FAIL] Navigation error: {e}\n")
            break

        html = page.content()

        if "Just a moment" in html or "challenge-platform" in html:
            print(f"  [BLOCKED] Cloudflare challenge page returned — {label} did not work.\n")
            break

        soup = BeautifulSoup(html, "html.parser")

        body_text = soup.get_text()
        m = re.search(r'(\d+)\s*-\s*(\d+)\s*of\s*(\d+)', body_text)
        if m:
            print(f"  Reported range: {m.group(0)}")
        else:
            print("  [WARN] Could not find the '1 - 100 of N' results counter on the page.")

        # Each match row's event name is the link text immediately preceding
        # the match link in the DOM — same pattern used elsewhere in CSRS.py.
        #
        # IMPORTANT: HLTV's /results page also has a "RECENT ACTIVITY" sidebar
        # widget further down the page that links to /matches/<id>/... URLs
        # too — some of which duplicate matches already in the main 100-item
        # list, others of which are entirely different matches not in this
        # page's range at all. Searching the whole page (soup.find_all(...))
        # picks up both, which is why earlier runs reported 107-110 "matches"
        # on a page HLTV itself says holds exactly 100. We scope the search
        # to the actual results container so the sidebar is excluded.
        results_container = soup.find("div", class_="results")
        search_root = results_container if results_container is not None else soup
        if results_container is None:
            print("  [WARN] Could not find the results container by class name — falling back to "
                  "whole-page search, counts may include sidebar links (e.g. RECENT ACTIVITY).")

        seen_on_page: set[str] = set()
        match_urls_on_page: list[str] = []
        for a in search_root.find_all("a", href=re.compile(r'^/matches/\d+/')):
            href = a.get("href", "")
            full_url = "https://www.hltv.org" + href
            if full_url not in seen_on_page:
                seen_on_page.add(full_url)
                match_urls_on_page.append(full_url)

        if not match_urls_on_page:
            print("  [WARN] No match links found on this page — may be blocked, empty, or layout changed.\n")
            break

        print(f"  Found {len(match_urls_on_page)} unique match URL(s) on this page. First 5:")
        for full_url in match_urls_on_page[:5]:
            print(f"    {full_url}")
        total_urls_seen += len(match_urls_on_page)

        print()
        _time.sleep(_random.uniform(1.0, 2.5))

    print(f"{label}: {total_urls_seen} total match URL(s) seen across up to {max_pages} page(s).\n")
    return total_urls_seen


def run_test_results_page(start_date: date, end_date: date | None = None, max_pages: int = 2):
    """
    Diagnostic-only test: hits the HLTV /results page directly — both the
    plain undated feed and the date-filtered variant — instead of walking
    events one at a time via /results?event=<id>, and reports what comes
    back from each. No importing, no checkpoints, nothing is saved — this
    is purely to find out whether either approach works on this machine
    before committing to rewriting the real backfill around it.

    Usage:
      python backfill.py --test-results-page
      python backfill.py --test-results-page --start 2026-01-01 --end 2026-06-29
    """
    if end_date is None:
        end_date = date.today()

    cookies = CSRS._load_hltv_cookies()
    pw_cookies = [
        {"name": c.get("name", ""), "value": c.get("value", ""),
         "domain": c.get("domain", ".hltv.org"), "path": c.get("path", "/")}
        for c in cookies if c.get("name") and c.get("value")
    ]

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        print("ERROR: Camoufox not installed. Run: pip install camoufox && python -m camoufox fetch")
        return

    print(f"\n{'='*60}")
    print(f"TEST: HLTV /results page (plain + date-filtered)")
    print(f"{'='*60}")
    print(f"Using {len(pw_cookies)} cookie(s) loaded from {CSRS.HLTV_COOKIE_FILE}\n")

    with Camoufox(headless=CSRS._CAMOUFOX_HEADLESS, os=("windows", "macos", "linux")) as browser:
        context = browser.new_context()
        if pw_cookies:
            context.add_cookies(pw_cookies)
        page = context.new_page()

        try:
            page.goto("https://www.hltv.org", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            print("Warm-up: OK")
        except Exception as e:
            print(f"Warm-up warning: {e}")

        plain_total = _test_one_results_variant(
            page,
            label="plain /results (no date filter)",
            base_url="https://www.hltv.org/results",
            max_pages=max_pages,
        )

        date_filtered_total = _test_one_results_variant(
            page,
            label=f"date-filtered /results?startDate={start_date}&endDate={end_date}",
            base_url=f"https://www.hltv.org/results?startDate={start_date.isoformat()}&endDate={end_date.isoformat()}",
            max_pages=max_pages,
        )

    print(f"{'='*60}")
    print(f"TEST COMPLETE")
    print(f"  Plain /results          : {plain_total} match URL(s) seen")
    print(f"  Date-filtered /results  : {date_filtered_total} match URL(s) seen")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CSRS results-page-offset backfill")
    parser.add_argument("--start", default=START_DATE.isoformat(), metavar="YYYY-MM-DD")
    parser.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                         help="End date for --test-results-page (defaults to today)")
    parser.add_argument("--test-results-page", action="store_true",
                         help="Diagnostic only: test the date-filtered /results page, no importing")
    parser.add_argument("--import", dest="do_import", action="store_true",
                         help="Real backfill: walk /results from --start to today and import matches")
    parser.add_argument("--dry-run", action="store_true",
                         help="With --import: collect URLs only, skip the actual import")
    parser.add_argument("--max-pages", type=int, default=1000, metavar="N",
                         help="Cap on number of /results pages to walk (default 1000)")
    parser.add_argument("--retry-failed", action="store_true",
                         help="Retry URLs in backfill_failed.log")
    args = parser.parse_args()

    if args.test_results_page:
        end = date.fromisoformat(args.end) if args.end else None
        run_test_results_page(date.fromisoformat(args.start), end)
    elif args.retry_failed:
        run_retry_failed()
    elif args.do_import:
        run_backfill(date.fromisoformat(args.start), dry_run=args.dry_run, max_pages=args.max_pages)
    else:
        print("Nothing to do. Use --test-results-page to diagnose, --import to run the real "
              "backfill, or --retry-failed to retry failed URLs. See -h for details.")