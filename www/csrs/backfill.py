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
  python backfill.py --tag-stages                         # backfill match_stage for existing entries
  python backfill.py --tag-stages --dry-run               # preview without saving
  python backfill.py --tag-stages --limit 10              # test on first 10 untagged entries
  python backfill.py --live                               # poll HLTV every 30min and import new matches
  python backfill.py --live --interval 15                 # poll every 15 minutes instead
"""

import json, os, sys, argparse, time
from datetime import date, datetime, timedelta

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
# Scrape-only timing instrumentation
# ---------------------------------------------------------------------------
# CSRS._import_url_list does a lot per match beyond the actual page scrape
# (team auto-registration, event-tier lookup, rating math, save). To isolate
# just "page open -> data collected" we monkeypatch CSRS.scrape_match_data
# with a timing wrapper. This works because CSRS._import_url_list calls
# scrape_match_data(...) unqualified, which Python resolves from the CSRS
# module's global namespace at call time — so reassigning CSRS.scrape_match_data
# here is picked up there without touching CSRS.py at all.

_real_scrape_match_data = CSRS.scrape_match_data
_last_scrape_duration: dict = {"seconds": None}


def _timed_scrape_match_data(url, context=None):
    t0 = time.monotonic()
    result = _real_scrape_match_data(url, context=context)
    _last_scrape_duration["seconds"] = time.monotonic() - t0
    return result


CSRS.scrape_match_data = _timed_scrape_match_data


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint():
    """
    Returns the active backfill checkpoint, or None if there isn't one.
    A checkpoint is written right after URL collection finishes for an
    --import run, and deleted once that run's URL list is fully imported
    (no failures, nothing left over). Its presence is what --continue
    looks for.
    """
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_checkpoint(start_date: date, collected_urls: list[str], phase: str = "importing",
                     next_offset: int | None = None, hit_boundary: bool = False,
                     end_date: date | None = None):
    """
    phase="collecting": URL collection (the /results page walk) was
        interrupted. collected_urls is whatever was gathered so far, and
        next_offset is where to resume paginating from.
    phase="importing": collection finished; collected_urls is the full,
        final target list, ready to be filtered against CSRS.history and
        imported.
    """
    os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
    state = {
        "phase": phase,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat() if end_date else None,
        "collected_urls": collected_urls,
        "next_offset": next_offset,
        "hit_boundary": hit_boundary,
        "saved_at": datetime.now().astimezone().isoformat(),
    }
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def clear_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


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

def _format_duration(seconds: float) -> str:
    """Human-readable HhMmSs duration string."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# How many recent per-match timings to average for the ETA. Kept short
# because a single new-team-registration or new-event-tier-lookup match
# can be much slower than a routine repeat-event match, and we want the
# estimate to track current conditions rather than the whole run's history.
ETA_ROLLING_WINDOW = 25

# Save progress to disk every N successful imports (mirrors the cadence
# CSRS._import_url_list normally uses internally for its own batch calls).
SAVE_EVERY_N_IMPORTS      = 5
BROWSER_RAM_LIMIT_MB      = 500   # restart BrowserSession when Camoufox RSS exceeds this


def _camoufox_ram_mb() -> float:
    """
    Returns the total RSS (MB) of all running firefox/camoufox processes.
    Uses psutil — returns 0.0 if psutil is not installed or no process found.
    """
    try:
        import psutil
        total = 0
        for proc in psutil.process_iter(['name', 'memory_info']):
            try:
                name = (proc.info['name'] or '').lower()
                if 'firefox' in name or 'camoufox' in name or 'geckodriver' in name:
                    total += proc.info['memory_info'].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total / (1024 * 1024)
    except ImportError:
        return 0.0


def _import_with_retry(
    urls: list[str],
    sess_context,
    event_id: int,
    max_passes: int = MAX_RETRY_PASSES,
    retry_delay: int = RETRY_DELAY_SECONDS,
    max_runtime_seconds: float | None = None,
) -> tuple[int, list[str], list[str]]:
    """
    Wraps CSRS._import_url_list with an automatic retry loop, importing one
    URL at a time so each match's actual scrape+import time can be measured
    individually.

    After every match, prints:
      - that match's scrape-only time vs. rest-of-import time
      - a rolling average over the last ETA_ROLLING_WINDOW matches
      - running total elapsed time for the whole call
      - estimated time remaining and a projected local finish timestamp

    If max_runtime_seconds is set, the loop checks elapsed time before
    starting each new match (never mid-match) and stops once the budget is
    exhausted, returning whatever URLs hadn't been attempted yet as a
    separate "not_attempted" list — distinct from URLs that were attempted
    and failed, since the latter went through CSRS's own retry/failure path
    and the former are simply untouched and should be retried fresh.

    Keeps retrying failed URLs until either:
      - all URLs succeed,
      - max_passes retry attempts are exhausted, or
      - the time budget runs out.

    Returns (total_imported, still_failed_urls, not_attempted_urls).
    """
    remaining = list(urls)
    total_imported = 0
    pass_num = 0

    total_to_do = len(urls)
    done_count = 0
    durations: list[float] = []
    run_start = time.monotonic()
    not_attempted: list[str] = []
    time_budget_hit = False

    while remaining and pass_num < max_passes:
        pass_num += 1
        if pass_num > 1:
            print(f"    [RETRY {pass_num}/{max_passes}] {len(remaining)} URL(s) — waiting {retry_delay}s...")
            time.sleep(retry_delay)

        still_failed_this_pass: list[str] = []
        url_iter = list(remaining)
        i = 0

        while i < len(url_iter):
            # Open a fresh BrowserSession — either first time or after a
            # RAM-triggered restart. We check memory after each match and
            # break out of the inner loop when it exceeds BROWSER_RAM_LIMIT_MB,
            # which closes the browser via __exit__ and loops back here to
            # open a clean one for the remaining URLs.
            with CSRS.BrowserSession() as sess:
                while i < len(url_iter):
                    url = url_iter[i]

                    if max_runtime_seconds is not None and (time.monotonic() - run_start) >= max_runtime_seconds:
                        not_attempted.extend(url_iter[i:])
                        time_budget_hit = True
                        print(
                            f"\n    [TIME LIMIT] {_format_duration(max_runtime_seconds)} budget reached after "
                            f"{done_count}/{total_to_do} match(es) — stopping cleanly. "
                            f"{len(not_attempted)} URL(s) untouched, will be saved for next run.\n"
                        )
                        break

                    t0 = time.monotonic()
                    _last_scrape_duration["seconds"] = None
                    count, failed = CSRS._import_url_list([url], sess.context)
                    elapsed = time.monotonic() - t0
                    scrape_elapsed = _last_scrape_duration["seconds"]

                    total_imported += count
                    if failed:
                        still_failed_this_pass.extend(failed)

                    done_count += 1
                    i += 1
                    durations.append(elapsed)

                    window = durations[-ETA_ROLLING_WINDOW:]
                    avg = sum(window) / len(window)
                    remaining_count = max(0, total_to_do - done_count)
                    eta_seconds = avg * remaining_count
                    total_elapsed = time.monotonic() - run_start
                    finish_dt = datetime.now().astimezone() + timedelta(seconds=eta_seconds)

                    status = "ok" if count else "FAIL"
                    if scrape_elapsed is not None:
                        scrape_part = f"scrape: {scrape_elapsed:.1f}s | rest: {max(0.0, elapsed - scrape_elapsed):.1f}s | total: {elapsed:.1f}s"
                    else:
                        scrape_part = f"total: {elapsed:.1f}s (scrape never ran — registration/early failure)"

                    budget_part = ""
                    if max_runtime_seconds is not None:
                        time_left = max(0.0, max_runtime_seconds - total_elapsed)
                        budget_part = f" | time budget left: {_format_duration(time_left)}"

                    # RAM check — silently restart browser if over limit
                    ram_mb = _camoufox_ram_mb()
                    needs_restart = ram_mb > 0 and ram_mb >= BROWSER_RAM_LIMIT_MB

                    print(
                        f"    [{done_count}/{total_to_do}] {status} | {scrape_part} "
                        f"| avg(last {len(window)}): {avg:.1f}s "
                        f"| elapsed: {_format_duration(total_elapsed)} "
                        f"| remaining: {remaining_count} (~{_format_duration(eta_seconds)}) "
                        f"| ETA finish: {finish_dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
                        f"{budget_part}"
                    )

                    if count and total_imported % SAVE_EVERY_N_IMPORTS == 0:
                        CSRS.save_all(silent=True)

                    if needs_restart:
                        break  # exits inner while → exits `with` → browser closed → outer while reopens

            if time_budget_hit:
                break

        if not still_failed_this_pass:
            print(f"    All {total_to_do} URL(s) imported successfully (pass {pass_num}).")
            remaining = []
            break

        print(f"    Pass {pass_num}: {len(still_failed_this_pass)} still failed.")
        remaining = still_failed_this_pass

    total_run_time = time.monotonic() - run_start
    print(
        f"\n    Total scrape+import time: {_format_duration(total_run_time)} "
        f"for {total_imported} imported / {done_count} attempted "
        f"({total_run_time / max(done_count, 1):.1f}s/match average)."
        + (f" {len(not_attempted)} not yet attempted (time budget)." if not_attempted else "")
        + "\n"
    )

    return total_imported, remaining, not_attempted


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


def _walk_results_pages(start_date: date, max_pages: int, start_offset: int = 0,
                         collected_urls: list[str] | None = None, end_date: date | None = None):
    """
    Walks the plain /results feed page by page (offset start_offset, +100,
    +200, ...), collecting match URLs whose timestamp is >= start_date and,
    if end_date is given, <= end_date (inclusive of that whole day). Stops
    paginating as soon as a page's matches drop below start_date, since
    /results is strictly newest-first — end_date only filters which matches
    get collected, it never stops pagination early (we may need to page
    past a bunch of too-new matches before reaching the end_date..start_date
    window).

    Saves a "collecting"-phase checkpoint after every page, so a crash here
    (browser/driver crash, network drop, closed terminal) loses at most one
    page's worth of progress (~100 matches) rather than the whole walk.
    --continue picks up from the saved next_offset.

    Returns (collected_urls, hit_boundary, crashed: bool). collected_urls is
    newest-first (not yet reversed) — the caller reverses once collection is
    fully done.
    """
    import random as _random

    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    start_unix_ms = int(start_dt.timestamp() * 1000)

    end_unix_ms = None
    if end_date is not None:
        # exclusive upper bound at the start of the day *after* end_date,
        # so the whole end_date calendar day is included
        end_dt = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=1)
        end_unix_ms = int(end_dt.timestamp() * 1000)

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
        return collected_urls or [], False, True

    collected_urls = list(collected_urls) if collected_urls else []
    seen_urls: set[str] = set(collected_urls)
    hit_boundary = False
    crashed = False

    try:
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

            page_num = start_offset // CSRS.HLTV_RESULTS_PAGE_SIZE
            pages_walked_this_session = 0

            while pages_walked_this_session < max_pages:
                offset = page_num * CSRS.HLTV_RESULTS_PAGE_SIZE
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
                page_skipped_too_new = 0
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
                        if m["unix_ms"] is not None and end_unix_ms is not None and m["unix_ms"] >= end_unix_ms:
                            page_skipped_too_new += 1
                            continue  # newer than --end, skip but keep paginating
                        if m["url"] not in seen_urls:
                            seen_urls.add(m["url"])
                            collected_urls.append(m["url"])
                            page_new += 1

                skip_note = f", skipped {page_skipped_too_new} too-new" if page_skipped_too_new else ""
                print(f"  +{page_new} match URL(s) in range (running total: {len(collected_urls)}){skip_note}\n")

                page_num += 1
                pages_walked_this_session += 1

                # Checkpoint after every page — at most one page's worth of
                # progress (~100 matches) is at risk from a crash from here on.
                save_checkpoint(start_date, collected_urls, phase="collecting",
                                 next_offset=page_num * CSRS.HLTV_RESULTS_PAGE_SIZE,
                                 hit_boundary=hit_boundary, end_date=end_date)

                if hit_boundary:
                    break

                time.sleep(_random.uniform(1.0, 2.5))

            try:
                page.close()
            except Exception as e:
                print(f"  [WARN] page.close() raised: {e} (continuing anyway)")

    except Exception as e:
        # Covers browser/driver-level crashes, including ones that surface
        # from Camoufox's own __exit__ (e.g. Browser.close() failing after
        # the underlying Node/Playwright driver has already died). Whatever
        # was collected up to the last per-page checkpoint is not lost.
        print(f"\n  [CRASH] Browser session failed: {e}")
        print(f"  {len(collected_urls)} URL(s) collected before the crash — already checkpointed.")
        crashed = True

    return collected_urls, hit_boundary, crashed


def run_backfill(start_date: date, dry_run: bool = False, max_pages: int = 1000,
                  max_runtime_minutes: float | None = None, end_date: date | None = None):
    """
    Real offset-based backfill: walks the plain /results feed page by page,
    collecting match URLs whose timestamp is >= start_date (and <= end_date
    if given), then imports them. See _walk_results_pages for the collection
    step.
    """
    range_desc = f"{start_date} to {end_date}" if end_date else f"{start_date} to today"
    print(f"\n{'='*60}")
    print(f"CSRS Backfill — /results offset walk, {range_desc}")
    print(f"{'='*60}\n")

    collected_urls, hit_boundary, crashed = _walk_results_pages(start_date, max_pages, end_date=end_date)

    if crashed:
        print(f"\nCollection crashed but progress is checkpointed. Run `python backfill.py --continue` to resume.")
        return

    # /results is newest-first; reverse so matches are imported oldest -> newest,
    # which matters because pts_before/pts_after are scraped relative to each
    # team's rating at the time of that match's import.
    collected_urls = list(reversed(collected_urls))

    print(f"{'='*60}")
    print(f"boundary_hit={hit_boundary}")
    print(f"Collected {len(collected_urls)} match URL(s), {range_desc} (oldest -> newest order).")
    print(f"{'='*60}\n")

    if dry_run:
        print("DRY RUN — not importing. First 10 URLs (oldest first):")
        for u in collected_urls[:10]:
            print(f"  {u}")
        return

    if not collected_urls:
        print("Nothing to import.")
        return

    save_checkpoint(start_date, collected_urls, phase="importing", end_date=end_date)

    _run_import_session(collected_urls, max_runtime_minutes=max_runtime_minutes)


def _run_import_session(collected_urls: list[str], max_runtime_minutes: float | None = None):
    """
    Shared by run_backfill (fresh --import) and run_continue (--continue):
    filters collected_urls against what's already in CSRS.history, imports
    whatever's left (respecting max_runtime_minutes), saves/pushes, logs any
    leftover URLs to backfill_failed.log, and either clears the checkpoint
    (if everything imported cleanly) or leaves it in place for the next
    --continue / --retry-failed.
    """
    CSRS.load_all()
    already_imported = CSRS.get_imported_urls(CSRS.history)
    new_urls = [u for u in collected_urls if u not in already_imported]
    print(f"Loaded {len(CSRS.history)} existing matches, {len(already_imported)} unique URLs.")
    print(f"{len(collected_urls) - len(new_urls)} already imported, {len(new_urls)} left to import.\n")

    if not new_urls:
        print("Nothing left to do — backfill complete.")
        clear_checkpoint()
        return

    try:
        count, still_failed, not_attempted = _import_with_retry(
            new_urls, None, event_id=0,
            max_runtime_seconds=(max_runtime_minutes * 60 if max_runtime_minutes else None),
        )
    except Exception as e:
        print(f"[ERROR] Import session failed: {e}")
        log_failed(new_urls, event_id=0)
        return

    CSRS._vrs_session_cache.clear()
    CSRS.save_all(silent=True)

    print(f"\nImported {count} match(es). {len(still_failed)} failed, {len(not_attempted)} not yet attempted.")
    leftover = still_failed + not_attempted
    if leftover:
        log_failed(leftover, event_id=0)
        print(f"Logged {len(leftover)} URL(s) to {FAILED_LOG}.")
        print(f"{len(leftover)} URL(s) still pending — run `python backfill.py --continue` to keep going.")
    else:
        print("All URLs in this checkpoint imported successfully.")
        clear_checkpoint()

    CSRS._git_push()

    # Resimulation is unconditional — always rebuild ratings from scratch
    print("\nResimulating all ratings in chronological order...")
    CSRS.resimulate()
    CSRS.save_all(silent=True)
    CSRS._git_push()
    print("Resimulation complete.\n")


def run_continue(max_runtime_minutes: float | None = None):
    """
    Resumes an in-progress backfill from its checkpoint — whichever phase it
    was in when it stopped:
      - "collecting": resumes the /results page walk from next_offset
        (no need to redo pages already walked), then proceeds to import.
      - "importing": collection was already done; re-filters the saved URL
        list against CSRS.history and imports whatever's left.
    """
    checkpoint = load_checkpoint()
    if not checkpoint:
        print("No active backfill checkpoint found. Start one with:")
        print("  python backfill.py --import --start YYYY-MM-DD")
        return

    phase = checkpoint.get("phase", "importing")  # old checkpoints had no phase field
    start_date = date.fromisoformat(checkpoint["start_date"])
    end_date = date.fromisoformat(checkpoint["end_date"]) if checkpoint.get("end_date") else None
    collected_urls = checkpoint.get("collected_urls", [])
    saved_at = checkpoint.get("saved_at", "?")

    if phase == "collecting":
        next_offset = checkpoint.get("next_offset") or 0
        range_desc = f"{start_date} to {end_date}" if end_date else f"{start_date} to today"
        print(f"\n{'='*60}")
        print(f"Resuming URL collection — {range_desc}, saved at {saved_at}")
        print(f"{len(collected_urls)} URL(s) already collected; resuming /results walk at offset={next_offset}")
        print(f"{'='*60}\n")

        collected_urls, hit_boundary, crashed = _walk_results_pages(
            start_date, max_pages=1000, start_offset=next_offset, collected_urls=collected_urls,
            end_date=end_date,
        )
        if crashed:
            print("\nCollection crashed again but progress is checkpointed. Run `python backfill.py --continue` to resume.")
            return

        collected_urls = list(reversed(collected_urls))
        print(f"{'='*60}")
        print(f"boundary_hit={hit_boundary}")
        print(f"Collected {len(collected_urls)} match URL(s) total (oldest -> newest order).")
        print(f"{'='*60}\n")

        if not collected_urls:
            print("Nothing to import.")
            clear_checkpoint()
            return

        save_checkpoint(start_date, collected_urls, phase="importing", end_date=end_date)
    else:
        print(f"\n{'='*60}")
        print(f"Resuming import — start={start_date}, saved at {saved_at}")
        print(f"{len(collected_urls)} URL(s) in this checkpoint's full target list.")
        print(f"{'='*60}\n")

    _run_import_session(collected_urls, max_runtime_minutes=max_runtime_minutes)


def run_retry_failed(max_runtime_minutes: float | None = None):
    entries = load_failed_urls()
    if not entries:
        print("No failed URLs in backfill_failed.log")
        return

    urls = [u for _, u in entries]
    print(f"Retrying {len(urls)} previously failed URL(s)...")
    CSRS.load_all()

    try:
        count, still_failed, not_attempted = _import_with_retry(
            urls, None, event_id=0,
            max_runtime_seconds=(max_runtime_minutes * 60 if max_runtime_minutes else None),
        )
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    CSRS._vrs_session_cache.clear()
    CSRS.save_all(silent=True)
    CSRS._git_push()

    leftover = still_failed + not_attempted
    print(f"Retry complete: {count} imported, {len(still_failed)} still failing, {len(not_attempted)} not yet attempted")
    if leftover:
        with open(FAILED_LOG, "w") as f:
            for u in leftover:
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

STAGE_STRINGS = [
    "grand final",
    "upper bracket final",
    "lower bracket final",
    "consolidation final",
    "3rd place decider",
    "upper bracket semi-final",
    "lower bracket semi-final",
    "semi-final",
    "upper bracket quarter-final",
    "lower bracket quarter-final",
    "quarter-final",
]

_STAGE_JS = """
() => {
    const el = document.querySelector(
        '.matchpage-versus-head-stagename, .stage-name, .match-stage'
    );
    const veto = document.querySelector('.veto-box .preformatted-text');
    return (el ? el.textContent : (veto ? veto.textContent : document.body.textContent)).toLowerCase();
}
"""

def _detect_stage(text: str):
    for s in STAGE_STRINGS:
        if s in text:
            return " ".join(w.capitalize() for w in s.split(" "))
    return None


def run_tag_stages(dry_run: bool = False, limit=None):
    """Retroactively scrape match_stage for entries that have a URL but no stage tag."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    # Use CSRS's own history list so save_all() picks up our changes
    history = CSRS.history

    to_do = [
        (i, m) for i, m in enumerate(history)
        if m.get("url") and m.get("match_stage") is None
    ]
    if limit:
        to_do = to_do[:limit]

    print(f"Entries without match_stage: {len(to_do)} (of {len(history)} total)")
    if dry_run:
        print("DRY RUN — data.save will not be modified.\n")
    if not to_do:
        print("Nothing to do.")
        return

    updated = tagged = errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        for n, (hi, match) in enumerate(to_do, 1):
            t1  = match.get("t1", {}).get("name", "?")
            t2  = match.get("t2", {}).get("name", "?")
            url = match["url"]
            print(f"[{n}/{len(to_do)}] {t1} vs {t2}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(1_500)
                raw   = page.evaluate(_STAGE_JS)
                stage = _detect_stage(raw)
                print(f"  → {stage or 'No stage detected'}")
                if not dry_run:
                    history[hi]["match_stage"] = stage if stage is not None else False
                updated += 1
                if stage:
                    tagged += 1
            except PWTimeout:
                print("  → TIMEOUT — skipping")
                errors += 1
            except Exception as e:
                print(f"  → ERROR: {e} — skipping")
                errors += 1

            time.sleep(1.2)

        browser.close()

    print(f"\nDone. Processed: {updated}  Tagged: {tagged}  Errors: {errors}")
    if not dry_run and updated:
        CSRS.save_all()
        print("Saved to data.save")
    elif dry_run:
        print("Dry run — no changes saved.")


def _walk_until_known(known_urls: set[str], max_pages: int = 20) -> list[str]:
    """
    Walks /results newest-first, collecting URLs until it hits one that's
    already in known_urls (i.e. the last imported match). Returns the
    collected URLs in newest-first order — caller reverses to oldest-first
    before importing.

    This is more reliable than a fixed date lookback for live mode: it
    always catches everything since the last import, regardless of how
    long the daemon was down, and stops exactly at the right place without
    fetching unnecessary pages.
    """
    import random as _random

    cookies = CSRS._load_hltv_cookies()
    pw_cookies = [
        {"name": c.get("name", ""), "value": c.get("value", ""),
         "domain": c.get("domain", ".hltv.org"), "path": c.get("path", "/")}
        for c in cookies if c.get("name") and c.get("value")
    ]

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        print("[LIVE] ERROR: Camoufox not installed.")
        return []

    collected: list[str] = []
    seen: set[str] = set()
    hit_known = False

    try:
        with Camoufox(headless=CSRS._CAMOUFOX_HEADLESS, os=("windows", "macos", "linux")) as browser:
            context = browser.new_context()
            if pw_cookies:
                context.add_cookies(pw_cookies)
            page = context.new_page()

            try:
                page.goto("https://www.hltv.org", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"[LIVE] Warm-up warning: {e}")

            for page_num in range(max_pages):
                offset = page_num * CSRS.HLTV_RESULTS_PAGE_SIZE
                url = ("https://www.hltv.org/results" if offset == 0
                       else f"https://www.hltv.org/results?offset={offset}")
                print(f"[LIVE] Page {page_num + 1} (offset={offset})")

                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"[LIVE] Navigation error: {e} — stopping")
                    break

                html = page.content()
                if "Just a moment" in html or "challenge-platform" in html:
                    print("[LIVE] Cloudflare block — stopping")
                    break

                groups = _parse_results_page(html)
                if not groups:
                    print("[LIVE] No day-groups found — end of feed")
                    break

                page_new = 0
                for grp in groups:
                    for m in grp["matches"]:
                        if m["url"] in known_urls:
                            # Hit a URL we already have — everything older is also known
                            print(f"[LIVE] Hit known URL after {len(collected)} new — stopping.")
                            hit_known = True
                            break
                        if m["url"] not in seen:
                            seen.add(m["url"])
                            collected.append(m["url"])
                            page_new += 1
                    if hit_known:
                        break
                if hit_known:
                    break

                print(f"[LIVE]   +{page_new} new URL(s) (running total: {len(collected)})")
                time.sleep(_random.uniform(1.0, 2.0))

            try:
                page.close()
            except Exception:
                pass

    except Exception as e:
        print(f"[LIVE] Browser error: {e}")

    return collected


def run_live(interval_minutes: int = 30, lookback_days: int = 2, smart_lookback: bool = False):
    """
    Continuously poll HLTV results and import any new matches.

    Two collection modes:
      Default (--lookback-days N): walks /results back N days each cycle.
      Smart (--smart-lookback):    walks /results until it hits the last
          already-imported URL — catches everything since the previous
          import with no fixed time window.

    Runs until interrupted with Ctrl+C.
    """
    import signal

    mode_desc = ("smart lookback (stop at last known URL)"
                 if smart_lookback else f"lookback {lookback_days} day(s)")
    print(f"[LIVE] Starting live import mode — polling every {interval_minutes} minute(s), "
          f"{mode_desc}. Ctrl+C to stop.\n")

    # Graceful shutdown on Ctrl+C
    _stop = {"flag": False}
    def _on_sigint(sig, frame):
        print("\n[LIVE] Interrupt received — finishing current cycle then stopping…")
        _stop["flag"] = True
    signal.signal(signal.SIGINT, _on_sigint)

    cycle = 0
    while not _stop["flag"]:
        cycle += 1
        cycle_start = datetime.now()
        print(f"\n{'═'*60}")
        print(f"[LIVE] Cycle {cycle} — {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'═'*60}")

        # Always reload so we pick up any external changes to data.save
        CSRS.load_all()
        known_urls: set[str] = {m.get("url", "") for m in CSRS.history if m.get("url")}

        print(f"[LIVE] {len(known_urls)} URLs already imported.\n")

        # Collect new URLs
        try:
            if smart_lookback:
                collected = _walk_until_known(known_urls)
            else:
                start_date = date.today() - timedelta(days=lookback_days)
                print(f"[LIVE] Checking results from {start_date} onwards…\n")
                collected, _, _ = _walk_results_pages(
                    start_date=start_date,
                    max_pages=5,
                )
        except Exception as e:
            print(f"[LIVE] Collection error: {e} — skipping this cycle")
            collected = []

        # Filter and reverse to oldest-first
        new_urls = [u for u in reversed(collected) if u not in known_urls]

        if not new_urls:
            print(f"[LIVE] No new matches found.")
        else:
            print(f"[LIVE] Found {len(new_urls)} new match(es) — importing…\n")
            try:
                count, still_failed, _ = _import_with_retry(new_urls, None, event_id=0)
                CSRS._vrs_session_cache.clear()
                CSRS.save_all(silent=True)
                CSRS._git_push()
                print(f"\n[LIVE] Cycle {cycle} done — imported {count}, failed {len(still_failed)}.")
                if still_failed:
                    log_failed(still_failed, event_id=0)
                    print(f"[LIVE] {len(still_failed)} failed URL(s) logged to backfill_failed.log")
            except Exception as e:
                print(f"[LIVE] Import error: {e}")

        if _stop["flag"]:
            break

        # Sleep until next cycle, waking every second to check for Ctrl+C
        next_run = cycle_start + timedelta(minutes=interval_minutes)
        print(f"\n[LIVE] Next check at {next_run.strftime('%H:%M:%S')} "
              f"(in {interval_minutes} minute(s)). Ctrl+C to stop.")
        while datetime.now() < next_run and not _stop["flag"]:
            time.sleep(1)

    print("\n[LIVE] Stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CSRS results-page-offset backfill")
    parser.add_argument("--start", default=START_DATE.isoformat(), metavar="YYYY-MM-DD")
    parser.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                         help="End date (inclusive). With --import, only matches on/before this "
                              "date are collected (defaults to today). Also used by --test-results-page.")
    parser.add_argument("--test-results-page", action="store_true",
                         help="Diagnostic only: test the date-filtered /results page, no importing")
    parser.add_argument("--import", dest="do_import", action="store_true",
                         help="Real backfill: walk /results from --start to --end (default: today) and import matches")
    parser.add_argument("--dry-run", action="store_true",
                         help="With --import: collect URLs only, skip the actual import")
    parser.add_argument("--max-pages", type=int, default=1000, metavar="N",
                         help="Cap on number of /results pages to walk (default 1000)")
    parser.add_argument("--retry-failed", action="store_true",
                         help="Retry URLs in backfill_failed.log")
    parser.add_argument("--continue", dest="do_continue", action="store_true",
                         help="Resume the in-progress --import checkpoint (no need to re-pass --start)")
    parser.add_argument("--max-runtime", type=float, default=None, metavar="MINUTES",
                         help="Stop the import session cleanly after this many minutes "
                              "(checked between matches, never mid-scrape). With --import or "
                              "--continue, untouched/failed URLs stay in the checkpoint/failed log "
                              "for the next --continue run.")
    parser.add_argument("--tag-stages", action="store_true",
                         help="Retroactively scrape match_stage for history entries that have a URL "
                              "but no match_stage field yet. Safe to re-run; already-tagged entries "
                              "are skipped.")
    parser.add_argument("--live", action="store_true",
                         help="Continuously poll HLTV results every 30 minutes and import any new "
                              "matches that haven't been imported yet. Runs indefinitely until "
                              "interrupted with Ctrl+C.")
    parser.add_argument("--interval", type=int, default=30, metavar="MINUTES",
                         help="With --live: polling interval in minutes (default: 30).")
    parser.add_argument("--lookback-days", type=int, default=2, metavar="DAYS",
                         help="With --live: how many days back to check for new results each cycle (default: 2).")
    parser.add_argument("--smart-lookback", action="store_true",
                         help="With --live: instead of a fixed date window, walk /results until "
                              "the last already-imported URL is found — always catches everything "
                              "since the previous import with no over-fetching.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                         help="With --tag-stages: only process the first N untagged entries (useful for testing).")
    args = parser.parse_args()

    if args.test_results_page:
        end = date.fromisoformat(args.end) if args.end else None
        run_test_results_page(date.fromisoformat(args.start), end)
    elif args.do_continue:
        run_continue(max_runtime_minutes=args.max_runtime)
    elif args.retry_failed:
        run_retry_failed(max_runtime_minutes=args.max_runtime)
    elif args.do_import:
        end = date.fromisoformat(args.end) if args.end else None
        run_backfill(date.fromisoformat(args.start), dry_run=args.dry_run, max_pages=args.max_pages,
                     max_runtime_minutes=args.max_runtime, end_date=end)
    elif args.tag_stages:
        run_tag_stages(dry_run=args.dry_run, limit=args.limit)
    elif args.live:
        run_live(interval_minutes=args.interval, lookback_days=args.lookback_days,
                 smart_lookback=args.smart_lookback)
    else:
        print("Nothing to do. Use --test-results-page to diagnose, --import to run the real "
              "backfill, --retry-failed to retry failed URLs, --tag-stages to backfill "
              "match stages, or --live for continuous polling. See -h for details.")