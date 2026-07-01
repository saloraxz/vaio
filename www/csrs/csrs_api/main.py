# -*- coding: utf-8 -*-
"""
CSRS Web API — FastAPI backend
Reads data.save (base64+zlib+json) and exposes REST endpoints
for rankings, history, analytics, simulation, and graphs.
"""

import base64
import zlib
import json
import math
import os
from datetime import datetime
from math import comb
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_FILE = Path(os.environ.get("CSRS_DATA_FILE", "data.save"))
FRONTEND_DIR = Path(os.environ.get("CSRS_FRONTEND_DIR", "frontend"))

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="CSRS API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data() -> dict:
    """Load and decode data.save → dict. Raises HTTPException on failure."""
    if not DATA_FILE.exists():
        raise HTTPException(status_code=503, detail=f"data.save not found at {DATA_FILE}")
    try:
        raw = DATA_FILE.read_bytes()
        decoded = base64.b64decode(raw)
        decompressed = zlib.decompress(decoded)
        return json.loads(decompressed)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read data.save: {e}")


def _compute_best_ever_ranks(history: list, teams: dict = None) -> dict:
    """
    Replay match history chronologically, recomputing full *depreciated*
    league standings after every match (mirroring how /api/rankings'
    current `rank` is computed — via _apply_depreciation, not raw points),
    and track each team's best (lowest-number) rank ever held.

    Also includes the live "now" snapshot (today's date, current stored
    points) as one additional candidate point in time — since depreciation
    is calendar-time-based, a team's rank right now can differ from its
    rank at any past match purely because *other* teams have decayed
    further since their own last match. Without this, a team could show a
    current rank better than any "peak rank" the replay ever recorded.

    This is independent of peak points — a team's points-peak day and its
    best-rank day are not necessarily the same day, since rank also depends
    on what every other team was doing (including their own inactivity
    depreciation) at that point in time.

    Depreciation-aware because an earlier raw-points-only version produced
    peak ranks that disagreed with current `rank` for inactive teams, since
    current `rank` already accounts for depreciation but raw points don't.

    `teams` (name -> current raw points) is needed for the "now" snapshot;
    if omitted, only match-history points in time are considered.
    """
    from datetime import datetime

    date_index = _build_match_date_index(history)

    def parse_date(date_str: str):
        clean = date_str.replace(" UTC", "").strip()
        try:
            return datetime.strptime(clean, "%Y-%m-%d %H:%M")
        except ValueError:
            return datetime.strptime(clean[:10], "%Y-%m-%d")

    running_raw_points: dict = {}
    best_rank: dict = {}

    for m in history:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            as_of = parse_date(date_str)
        except Exception:
            continue

        for side in ("t1", "t2"):
            name = m[side]["name"]
            running_raw_points[name] = m[side]["pts_after"]

        # Recompute depreciated standings for every team seen so far, as of
        # this match's date — same logic /api/rankings uses for "today".
        depreciated: dict = {}
        for name, raw_pts in running_raw_points.items():
            depreciated[name] = _apply_depreciation(
                name, raw_pts, match_date=as_of, index=date_index
            )

        ranked = sorted(depreciated.items(), key=lambda x: x[1], reverse=True)
        for pos, (name, _pts) in enumerate(ranked, 1):
            if name not in best_rank or pos < best_rank[name]:
                best_rank[name] = pos

    # Final candidate point: "now" — same depreciation logic /api/rankings
    # uses for current display rank (last match with no before_date cap,
    # i.e. each team's true most recent match, not "most recent before X").
    if teams:
        today = datetime.now()
        team_display: dict = {}
        for name, pts in teams.items():
            last = _get_last_match_date(name, index=date_index)
            if last is None:
                team_display[name] = pts
                continue
            days_inactive = (today - last).days
            team_display[name] = _calculate_depreciation(pts, days_inactive, team_name=name)

        ranked_now = sorted(team_display.items(), key=lambda x: x[1], reverse=True)
        for pos, (name, _pts) in enumerate(ranked_now, 1):
            if name not in best_rank or pos < best_rank[name]:
                best_rank[name] = pos

    return best_rank


_best_rank_cache: dict = {"mtime": None, "result": None}


def _compute_best_ever_ranks_cached(history: list, teams: dict = None) -> dict:
    """
    Cached wrapper around _compute_best_ever_ranks. Recomputes only when
    DATA_FILE's modification time changes (i.e. data.save was updated),
    since the underlying replay is too expensive to redo on every request.

    Note: the "now" snapshot inside _compute_best_ever_ranks is evaluated
    once at cache-computation time, not freshly on every request — so
    between data updates, a team's best-ever rank won't keep improving in
    real time purely from elapsed-day depreciation of its rivals. It will
    refresh the next time data.save changes and the cache invalidates.
    """
    try:
        mtime = DATA_FILE.stat().st_mtime
    except OSError:
        mtime = None

    if _best_rank_cache["mtime"] == mtime and _best_rank_cache["result"] is not None:
        return _best_rank_cache["result"]

    result = _compute_best_ever_ranks(history, teams=teams)
    _best_rank_cache["mtime"] = mtime
    _best_rank_cache["result"] = result
    return result


# ---------------------------------------------------------------------------
# Elo simulation — exact port of CSRS.py calculate_points()
# ---------------------------------------------------------------------------

# Constants — must match DEFAULT_CONFIG in CSRS.py
RATING_CAP   = 12930
RATING_FLOOR = 0
K_WIN        = 33
K_LOSS       = 22
PROVISIONAL_OPP_DIFF_CAP = 200

# Provisional team's OWN change multiplier — applied on top of opponent cap.
# Match 1 = 3.0x, match 2 = 2.5x, match 3 = 2.0x, then graduates.
PROVISIONAL_MATCH_THRESHOLD = 3
PROVISIONAL_K_FACTORS = {1: 3.0, 2: 2.5, 3: 2.0}

DIMINISHING_RETURNS_ENABLED  = True
DIMINISHING_THRESHOLD        = 1000
DIMINISHING_MAX              = 1050
DIMINISHING_K_WIN_MIN_PCT    = 0.0
DIMINISHING_K_LOSS_MIN_PCT   = 1.0

MISMATCH_PENALTY_ENABLED   = True
MISMATCH_DECAY_PERCENT     = 1.0
MISMATCH_ZERO_POINT_PERCENT = 0.70
MISMATCH_MAX_PENALTY_PERCENT = 0.66
MISMATCH_MAX_PENALTY_VALUE  = -0.75

PITY_POINTS_ENABLED    = True
PITY_THRESHOLD_PERCENT = 0.75
PITY_MAX_PERCENT       = 0.65
PITY_MAX_POINTS        = 9
PITY_MIN_POINTS        = 6

# Elite tier — exact port of DEFAULT_CONFIG["ELITE_THRESHOLD"] in CSRS.py.
ELITE_THRESHOLD = 850

# Form + depreciation — exact port of DEFAULT_CONFIG in CSRS.py.
# Used to compute the day-before-match depreciation transition point
# on the Elite Teams Over Time graph.
FORM_WIN_WEIGHT               = 42.5
FORM_MAP_WEIGHT               = 42.5
FORM_COMP_WEIGHT              = 15.0
FORM_STREAK_BONUS_ENABLED     = True
FORM_STREAK_BONUS_MAX         = 10.0
FORM_STREAK_BONUS_PER_WIN     = 2
FORM_STREAK_LOSS_RESET_COUNT  = 2
FORM_MODIFIER_MIN             = 0.5
FORM_MODIFIER_MAX             = 1.5
FORM_DIMINISHING_ENABLED      = True
FORM_DIMINISHING_THRESHOLD    = 0.85
FORM_DIMINISHING_COMPRESSION  = 0.67
DEPRECIATION_THRESHOLD        = 14

TIERS  = {"S+": 1.5, "S": 1.4, "A": 1.2, "B": 1.0, "C": 0.8, "D": 0.55}
MAPS   = {1: 0.8, 2: 1.0, 3: 1.2}
ENVS   = {"ONLINE": 0.8, "LAN": 1.1, "STUDIO": 1.1, "STAGE": 1.1}


def _calculate_points(
    team_pts: float,
    opp_pts: float,
    result: int,        # 1 = win, 0 = loss
    map_diff: int,      # maps won by winner (1, 2, or 3)
    tier: str = "A",
    env: str = "LAN",
    is_grand_final: bool = False,
    team_form_adj: float = 0.0,
    opp_form_adj: float = 0.0,
    opp_is_provisional: bool = False,
) -> float:
    """Exact port of CSRS.py calculate_points(). Returns new rating."""

    # Diminishing returns on K
    k_win  = K_WIN
    k_loss = K_LOSS
    if DIMINISHING_RETURNS_ENABLED and team_pts >= DIMINISHING_THRESHOLD:
        pos = max(0.0, min(1.0,
            (team_pts - DIMINISHING_THRESHOLD) / (DIMINISHING_MAX - DIMINISHING_THRESHOLD)
        ))
        k_win  = K_WIN  * (1.0 - pos * (1.0 - DIMINISHING_K_WIN_MIN_PCT))
        k_loss = K_LOSS * (1.0 - pos * (1.0 - DIMINISHING_K_LOSS_MIN_PCT))

    K = k_win if result == 1 else k_loss

    m_tier = TIERS.get(tier.upper(), 1.0)
    m_map  = MAPS.get(map_diff, 1.0)
    m_env  = ENVS.get(env.upper(), 1.0)
    gf_mult = 1.5 if is_grand_final else 1.0

    # Effective ratings with form adjustments
    team_eff = team_pts + team_form_adj
    opp_eff  = opp_pts  + opp_form_adj

    # Provisional opponent rating cap
    if opp_is_provisional:
        diff = opp_eff - team_eff
        if abs(diff) > PROVISIONAL_OPP_DIFF_CAP:
            opp_eff = team_eff + PROVISIONAL_OPP_DIFF_CAP * (1 if diff > 0 else -1)

    expected = 1 / (1 + 10 ** ((opp_eff - team_eff) / 400))

    # Upset factor
    if team_eff > opp_eff and result == 0:
        upset = team_eff / opp_eff if opp_eff > 0 else 1.0
    elif team_eff < opp_eff and result == 1:
        upset = opp_eff / team_eff if team_eff > 0 else 1.0
    else:
        upset = 1.0

    change = K * (result - expected) * m_tier * m_map * gf_mult * upset

    # Pity points (underdog losses)
    pity_bonus = 0.0
    if PITY_POINTS_ENABLED and result == 0:
        team_rating_pct = team_pts / opp_pts if opp_pts > 0 else 0
        if team_rating_pct <= PITY_THRESHOLD_PERCENT:
            gap_factor = min(1.0,
                (PITY_THRESHOLD_PERCENT - team_rating_pct) /
                (PITY_THRESHOLD_PERCENT - PITY_MAX_PERCENT)
            )
            pity_base = {1: 9, 2: 6, 3: 3}.get(map_diff, 3)
            pity_bonus = pity_base * gap_factor * m_tier * m_env * gf_mult
            if pity_bonus < PITY_MIN_POINTS:
                pity_bonus = float(PITY_MIN_POINTS)

            gap_from_threshold = PITY_THRESHOLD_PERCENT - team_rating_pct
            total_range = PITY_THRESHOLD_PERCENT - PITY_MAX_PERCENT
            scale_pos = min(1.0, gap_from_threshold / total_range)
            target_net = 0 + scale_pos * 7
            if change + pity_bonus < target_net:
                pity_bonus = target_net - change

    # Mismatch penalty (beating much weaker teams)
    mismatch_mult = 1.0
    if MISMATCH_PENALTY_ENABLED and result == 1 and team_pts > 0:
        opp_pct = opp_pts / team_pts
        if opp_pct <= MISMATCH_DECAY_PERCENT:
            if opp_pct > MISMATCH_ZERO_POINT_PERCENT:
                pos = (opp_pct - MISMATCH_DECAY_PERCENT) / (MISMATCH_ZERO_POINT_PERCENT - MISMATCH_DECAY_PERCENT)
                mismatch_mult = 1.0 - pos
            elif opp_pct > MISMATCH_MAX_PENALTY_PERCENT:
                pos = (opp_pct - MISMATCH_ZERO_POINT_PERCENT) / (MISMATCH_MAX_PENALTY_PERCENT - MISMATCH_ZERO_POINT_PERCENT)
                mismatch_mult = 0.0 - pos * abs(MISMATCH_MAX_PENALTY_VALUE)
            else:
                mismatch_mult = MISMATCH_MAX_PENALTY_VALUE

    if result == 1:
        change *= m_env
        change *= mismatch_mult
    elif result == 0:
        change += pity_bonus

    new_rating = team_pts + change
    return float(max(RATING_FLOOR, min(RATING_CAP, new_rating)))


def _build_match_date_index(history_list: list) -> dict:
    """
    Build {team_name: sorted_list_of_datetimes} index from history.
    Mirrors CSRS.py build_match_date_index — used to avoid O(n) scans per team.
    """
    from datetime import datetime
    index: dict = {}
    for m in history_list:
        t1_name = m.get("t1", {}).get("name")
        t2_name = m.get("t2", {}).get("name")
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            clean = date_str.replace(" UTC", "").strip()
            try:
                match_date = datetime.strptime(clean, "%Y-%m-%d %H:%M")
            except ValueError:
                match_date = datetime.strptime(clean[:10], "%Y-%m-%d")
        except Exception:
            continue
        for name in (t1_name, t2_name):
            if name:
                index.setdefault(name, []).append(match_date)
    for name in index:
        index[name].sort()
    return index


def _get_last_match_date(team_name: str,
                         before_date=None,
                         index: dict = None,
                         history_list: list = None):
    """
    Return the most recent match datetime for *team_name* that is strictly
    before *before_date*.  If *before_date* is None, return the last match.
    Pass *index* (from _build_match_date_index) for O(log n) lookups.
    """
    import bisect
    from datetime import datetime

    if index is not None:
        dates = index.get(team_name)
        if not dates:
            return None
        if before_date is None:
            return dates[-1]
        idx = bisect.bisect_left(dates, before_date)
        return dates[idx - 1] if idx > 0 else None

    # Fallback: linear scan
    if history_list is None:
        return None
    team_dates = []
    for m in history_list:
        if m.get("t1", {}).get("name") != team_name and m.get("t2", {}).get("name") != team_name:
            continue
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            clean = date_str.replace(" UTC", "").strip()
            try:
                md = datetime.strptime(clean, "%Y-%m-%d %H:%M")
            except ValueError:
                md = datetime.strptime(clean[:10], "%Y-%m-%d")
            if before_date is None or md < before_date:
                team_dates.append(md)
        except Exception:
            continue
    return max(team_dates) if team_dates else None


def _apply_depreciation(team_name: str,
                        current_rating: float,
                        match_date=None,
                        index: dict = None,
                        history_list: list = None,
                        form_score: float = None) -> float:
    """
    Mirrors CSRS.py apply_depreciation_to_rating + calculate_depreciation.
    Returns the depreciated rating before a match (or before 'now' for display).
    Pass form_score to skip an extra form calculation.
    """
    last_match = _get_last_match_date(team_name,
                                      before_date=match_date,
                                      index=index,
                                      history_list=history_list)
    if last_match is None:
        return current_rating

    if match_date is None:
        from datetime import datetime
        match_date = datetime.now()

    days_inactive = (match_date - last_match).days
    return _calculate_depreciation(current_rating, days_inactive,
                                   team_name=team_name,
                                   match_index=None,
                                   history=None)


def _win_probability(r1_eff: float, r2_eff: float) -> float:
    """Win probability for team 1 given effective ratings."""
    return 1 / (1 + 10 ** ((r2_eff - r1_eff) / 400))


def _series_win_prob(p_map: float, bo: int) -> float:
    """Probability of winning a best-of-N series given per-map win probability."""
    wins_needed = (bo // 2) + 1
    total = 0.0
    for losses in range(wins_needed):
        # win in exactly (wins_needed + losses) maps
        total += comb(wins_needed + losses - 1, losses) * (p_map ** wins_needed) * ((1 - p_map) ** losses)
    return total


def simulate_elo(
    r1: float, r2: float,
    tier: str, env: str,
    grand_final: bool = False,
    bo: int = 3,
    form_adj_1: float = 0.0,
    form_adj_2: float = 0.0,
    t1_provisional: bool = False,
    t2_provisional: bool = False,
    t1_provisional_matches: int = 0,
    t2_provisional_matches: int = 0,
) -> dict:
    """
    Full simulation matching CSRS.py simulate_match() output.
    Returns win probabilities and point deltas for every possible scoreline.

    Provisional teams get their OWN rating change multiplied by 2x-3x
    (match 1 = 3.0x, match 2 = 2.5x, match 3 = 2.0x) on top of the
    opponent-rating-cap effect, exactly matching CSRS.py's live import flow.
    """
    wins_needed = (bo // 2) + 1

    # Effective ratings for win probability
    eff1 = r1 + form_adj_1
    eff2 = r2 + form_adj_2
    p_map = _win_probability(eff1, eff2)
    series_prob_t1 = _series_win_prob(p_map, bo)

    # Provisional K-multiplier — about to play match (matches_played + 1)
    t1_k = PROVISIONAL_K_FACTORS.get(t1_provisional_matches + 1, 1.0) if t1_provisional else 1.0
    t2_k = PROVISIONAL_K_FACTORS.get(t2_provisional_matches + 1, 1.0) if t2_provisional else 1.0

    def apply_provisional_k(new_rating: float, before: float, k: float) -> float:
        if k == 1.0:
            return new_rating
        return min(max(RATING_FLOOR, before + (new_rating - before) * k), RATING_CAP)

    # All possible scorelines
    scorelines = []
    for loser_maps in range(wins_needed):
        # t1 wins
        map_diff = wins_needed - loser_maps  # maps won by winner
        new_r1 = _calculate_points(r1, r2, 1, map_diff, tier, env, grand_final,
                                   form_adj_1, form_adj_2, t2_provisional)
        new_r2 = _calculate_points(r2, r1, 0, map_diff, tier, env, grand_final,
                                   form_adj_2, form_adj_1, t1_provisional)
        new_r1 = apply_provisional_k(new_r1, r1, t1_k)
        new_r2 = apply_provisional_k(new_r2, r2, t2_k)
        scorelines.append({
            "score": f"{wins_needed}-{loser_maps}",
            "winner": "t1",
            "t1_delta": round(new_r1 - r1, 2),
            "t2_delta": round(new_r2 - r2, 2),
            "t1_new":   round(new_r1, 2),
            "t2_new":   round(new_r2, 2),
        })

        # t2 wins
        new_r2b = _calculate_points(r2, r1, 1, map_diff, tier, env, grand_final,
                                    form_adj_2, form_adj_1, t1_provisional)
        new_r1b = _calculate_points(r1, r2, 0, map_diff, tier, env, grand_final,
                                    form_adj_1, form_adj_2, t2_provisional)
        new_r2b = apply_provisional_k(new_r2b, r2, t2_k)
        new_r1b = apply_provisional_k(new_r1b, r1, t1_k)
        scorelines.append({
            "score": f"{loser_maps}-{wins_needed}",
            "winner": "t2",
            "t1_delta": round(new_r1b - r1, 2),
            "t2_delta": round(new_r2b - r2, 2),
            "t1_new":   round(new_r1b, 2),
            "t2_new":   round(new_r2b, 2),
        })

    return {
        "win_probability_t1": round(series_prob_t1 * 100, 1),
        "win_probability_t2": round((1 - series_prob_t1) * 100, 1),
        "scorelines": scorelines,
    }


# ---------------------------------------------------------------------------
# Rankings endpoint
# ---------------------------------------------------------------------------

@app.get("/api/rankings")
def get_rankings(
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", description="Filter by team name"),
    as_of: str = Query("", description="View rankings as of YYYY-MM-DD. Defaults to today."),
):
    from collections import defaultdict
    from datetime import timedelta

    data = load_data()
    teams: dict = data.get("teams", {})
    peaks: dict = data.get("peaks", {})
    provisional: dict = data.get("provisional_teams", {})
    history: list = data.get("history", [])
    best_ever_rank: dict = _compute_best_ever_ranks_cached(history, teams=teams)

    today = datetime.now()

    # Parse as_of — default to today (live)
    if as_of:
        try:
            as_of_dt = datetime.strptime(as_of.strip(), "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD")
    else:
        as_of_dt = today

    is_historical = as_of_dt.date() < today.date()

    def _parse_entry_dt(date_str):
        """Parse a history entry date like '2026-03-18 10:00 UTC'."""
        if not date_str or date_str == "N/A":
            return None
        s = date_str.replace(" UTC", "").strip()
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M")
        except ValueError:
            pass
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None

    if is_historical:
        # Replay history to reconstruct ratings/last-match for every team
        # as they stood at end-of-day on as_of_dt.
        ref_teams: dict = {}       # name -> pts_after as of as_of_dt
        ref_last_match: dict = {}  # name -> most recent match datetime as of as_of_dt
        ref_peak: dict = {}        # name -> {"points": float, "date": str}

        for entry in history:
            match_dt = _parse_entry_dt(entry.get("date", ""))
            if match_dt is None or match_dt > as_of_dt:
                continue
            for side in ("t1", "t2"):
                name = entry[side]["name"]
                pts_after = entry[side]["pts_after"]
                prev_last = ref_last_match.get(name)
                if prev_last is None or match_dt >= prev_last:
                    ref_teams[name] = pts_after
                    ref_last_match[name] = match_dt
                if name not in ref_peak or pts_after > ref_peak[name]["points"]:
                    ref_peak[name] = {"points": pts_after, "date": entry.get("date", "")}

        ref_today = as_of_dt
    else:
        ref_teams = teams
        date_index = _build_match_date_index(history)
        ref_last_match = {
            name: _get_last_match_date(name, index=date_index)
            for name in teams
        }
        ref_peak = peaks
        ref_today = today

    # Sparkline: ratings over the 30 days leading up to as_of_dt
    team_spark: dict = defaultdict(list)
    spark_cutoff = (as_of_dt - timedelta(days=30)).strftime("%Y-%m-%d")
    as_of_str = as_of_dt.strftime("%Y-%m-%d")
    for entry in history:
        d10 = entry.get("date", "")[:10]
        if d10 < spark_cutoff or d10 > as_of_str:
            continue
        for side in ("t1", "t2"):
            team_spark[entry[side]["name"]].append(round(entry[side]["pts_after"], 2))

    # Depreciate ratings relative to ref_today
    team_display: dict = {}
    for name, pts in ref_teams.items():
        last = ref_last_match.get(name)
        days_inactive = max(0, (ref_today - last).days) if last else 0
        team_display[name] = _calculate_depreciation(pts, days_inactive, team_name=name)

    ranked = sorted(ref_teams.items(), key=lambda x: team_display[x[0]], reverse=True)

    results = []
    for rank, (name, pts) in enumerate(ranked, 1):
        if search and search.lower() not in name.lower():
            continue
        dep_pts = team_display[name]
        dep_loss = round(pts - dep_pts, 2)
        last_match = ref_last_match.get(name)
        days_inactive = max(0, (ref_today - last_match).days) if last_match else 0
        peak_info = ref_peak.get(name, {})
        results.append({
            "rank": rank,
            "name": name,
            "points": round(dep_pts, 2),
            "raw_points": round(pts, 2),
            "depreciation_loss": dep_loss if dep_loss > 0 else 0,
            "days_inactive": days_inactive,
            "peak_points": round(peak_info.get("points", pts), 2),
            "peak_date": peak_info.get("date"),
            "peak_rank": best_ever_rank.get(name),
            "provisional": name in provisional,
            "matches_until_ranked": provisional.get(name, 0) if name in provisional else None,
            "sparkline": team_spark.get(name, []),
        })

    return {"total": len(results), "rankings": results[:limit], "as_of": as_of_str}


# ---------------------------------------------------------------------------
# Team detail + rating history
# ---------------------------------------------------------------------------

@app.get("/api/team/{team_name}")
def get_team(team_name: str):
    from datetime import datetime, timedelta

    data = load_data()
    teams: dict = data.get("teams", {})
    history: list = data.get("history", [])
    peaks: dict = data.get("peaks", {})
    aliases: dict = data.get("aliases", {})

    # resolve alias
    resolved = aliases.get(team_name.lower(), team_name)
    matched = next((t for t in teams if t.lower() == resolved.lower()), None)
    if not matched:
        raise HTTPException(status_code=404, detail=f"Team '{team_name}' not found")

    pts = teams[matched]
    date_index = _build_match_date_index(history)
    today = datetime.now()
    cutoff_3m = today - timedelta(days=90)

    # Depreciation
    last_match_dt = _get_last_match_date(matched, index=date_index)
    days_inactive = (today - last_match_dt).days if last_match_dt else 0
    dep_pts = _calculate_depreciation(pts, days_inactive, team_name=matched, history=history)
    dep_loss = round(pts - dep_pts, 2)

    # Rank by depreciated ratings
    team_dep = {}
    for n, p in teams.items():
        lm = _get_last_match_date(n, index=date_index)
        di = (today - lm).days if lm else 0
        team_dep[n] = _calculate_depreciation(p, di, team_name=n, history=history)
    ranked_dep = sorted(team_dep.items(), key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (n, _) in enumerate(ranked_dep) if n == matched), None)

    # Build full timeline
    timeline = []
    for gi, entry in enumerate(history):
        side = None
        if entry["t1"]["name"] == matched:
            side = "t1"
        elif entry["t2"]["name"] == matched:
            side = "t2"
        if not side:
            continue

        opp_side = "t2" if side == "t1" else "t1"
        me = entry[side]
        opp = entry[opp_side]
        won = me["score"] > opp["score"]

        timeline.append({
            "_gi": gi,  # internal only — stripped before the response is returned
            "date": entry["date"],
            "event": entry["event"],
            "opponent": opp["name"],
            "score": f"{me['score']}-{opp['score']}",
            "won": won,
            "pts_before": round(me["pts_before"], 2),
            "pts_after": round(me["pts_after"], 2),
            "pts_delta": round(me["pts_after"] - me["pts_before"], 2),
            "tier": entry["tier"],
            "env": entry.get("env", "LAN"),
            "url": entry.get("url"),
        })

    # 3-month filtered subset
    def parse_date(s):
        try:
            return datetime.strptime(s.replace(" UTC", "").strip()[:10], "%Y-%m-%d")
        except Exception:
            return None

    timeline_3m = [
        m for m in timeline
        if (pd := parse_date(m["date"])) and pd >= cutoff_3m
    ]

    # All-time stats
    total_matches = len(timeline)
    wins_all      = sum(1 for t in timeline if t["won"])
    losses_all    = total_matches - wins_all

    # 3-month stats
    wins_3m   = sum(1 for t in timeline_3m if t["won"])
    losses_3m = len(timeline_3m) - wins_3m
    pts_delta_3m = round(
        timeline_3m[-1]["pts_after"] - timeline_3m[0]["pts_before"], 2
    ) if timeline_3m else 0.0

    # Form — only from matches within the 3-month window (max 15)
    # Build a history slice containing only 3m matches, preserving global indices
    # so _calculate_form_at_match_index gets the right pts_before values.
    # Easiest: find the global index of the first 3m match, then call with that slice.
    first_3m_global_idx = None
    for gi, entry in enumerate(history):
        t1n = entry.get("t1", {}).get("name")
        t2n = entry.get("t2", {}).get("name")
        if t1n != matched and t2n != matched:
            continue
        pd = parse_date(entry.get("date", ""))
        if pd and pd >= cutoff_3m:
            first_3m_global_idx = gi
            break

    form_data = None
    if first_3m_global_idx is not None:
        # Restrict streak/score to 3-month-window matches only
        form_n = len(timeline_3m) if timeline_3m else 15
        form_3m = _calculate_form_at_match_index(
            matched, len(history), history,
            n=form_n
        )
        if form_3m:
            grade, score, streak = form_3m
            recent_5 = streak[-5:] if len(streak) >= 5 else streak
            form_data = {
                "grade": grade,
                "score": round(score, 1),
                "streak": streak,
                "recent": recent_5,
            }

        # Attach form score/grade to each timeline entry too, so any chart
        # built from `timeline`/`timeline_3m` can plot form-over-time without
        # a second API shape. Same n-window convention as the snapshot above,
        # so the final point always matches the "Form (3 months)" stat card.
        for entry in timeline_3m:
            gi = entry["_gi"]
            if gi < first_3m_global_idx:
                continue
            f = _calculate_form_at_match_index(matched, gi + 1, history, n=form_n)
            if f:
                entry["form_score"] = round(f[1], 1)
                entry["form_grade"] = f[0]

    # Strip the internal global-index marker before it ever reaches the response
    for entry in timeline:
        entry.pop("_gi", None)

    peak_info = dict(peaks.get(matched, {}))
    peak_info["rank"] = _compute_best_ever_ranks_cached(history, teams=teams).get(matched, peak_info.get("rank"))

    # Day-by-day expanded series (90 days) with depreciation between matches
    daily_series: list = []
    if timeline_3m:
        match_lookup: dict = {}
        form_lookup:  dict = {}
        for m in timeline_3m:
            dk = m["date"][:10]
            match_lookup[dk] = m
            if m.get("form_score") is not None:
                form_lookup[dk] = (m["form_score"], m.get("form_grade", ""))

        # Carry rating from the last match before the window
        carried_rating_val = None
        for m in timeline:
            pd_ = parse_date(m["date"])
            if pd_ and pd_ < cutoff_3m:
                carried_rating_val = m["pts_after"]
        if carried_rating_val is None and timeline_3m:
            carried_rating_val = timeline_3m[0]["pts_before"]

        current_day      = cutoff_3m.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day          = today.replace(hour=0, minute=0, second=0, microsecond=0)
        current_rating   = carried_rating_val
        last_match_day   = cutoff_3m

        # Seed last_match_day to the actual last match before the window
        for m in reversed(timeline):
            pd_ = parse_date(m["date"])
            if pd_ and pd_ < cutoff_3m:
                last_match_day = pd_
                break

        current_form = None
        current_form_grade = None

        while current_day <= end_day:
            dk = current_day.strftime("%Y-%m-%d")
            is_match = dk in match_lookup
            if is_match:
                m = match_lookup[dk]
                current_rating = m["pts_after"]
                last_match_day = current_day
                if dk in form_lookup:
                    current_form, current_form_grade = form_lookup[dk]

            days_inactive = (current_day - last_match_day).days
            display_rating = _calculate_depreciation(
                current_rating, days_inactive, team_name=matched, history=history
            )

            daily_series.append({
                "date":       dk,
                "pts":        round(display_rating, 2),
                "match":      is_match,
                "won":        match_lookup[dk]["won"] if is_match else None,
                "opponent":   match_lookup[dk]["opponent"] if is_match else None,
                "score":      match_lookup[dk]["score"] if is_match else None,
                "pts_delta":  match_lookup[dk]["pts_delta"] if is_match else None,
                "form_score": current_form,
                "form_grade": current_form_grade,
                "deprecated": days_inactive > DEPRECIATION_THRESHOLD,
                "transition": False,
            })
            current_day += timedelta(days=1)

        # Mark transition points (day before each match after a gap > 1 day)
        for i in range(1, len(daily_series)):
            if daily_series[i]["match"]:
                prev_match_i = next(
                    (j for j in range(i - 1, -1, -1) if daily_series[j]["match"]),
                    None
                )
                gap = (i - prev_match_i) if prev_match_i is not None else i
                if gap > 1:
                    daily_series[i - 1]["transition"] = True

    return {
        "name": matched,
        "points": round(dep_pts, 2),
        "raw_points": round(pts, 2),
        "depreciation_loss": dep_loss if dep_loss > 0 else 0,
        "days_inactive": days_inactive,
        "rank": rank,
        "peak": peak_info,
        "form": form_data,
        # All-time
        "total_matches": total_matches,
        "wins": wins_all,
        "losses": losses_all,
        # 3-month window
        "wins_3m": wins_3m,
        "losses_3m": losses_3m,
        "pts_delta_3m": pts_delta_3m,
        "timeline": timeline,
        "timeline_3m": timeline_3m,
        "daily_series": daily_series,
    }


# ---------------------------------------------------------------------------
# Match history
# ---------------------------------------------------------------------------

@app.get("/api/history")
def get_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    team: str = Query("", description="Filter by team name"),
    tier: str = Query("", description="Filter by tier A/B/C/D/R/S"),
    event: str = Query("", description="Filter by event name"),
):
    data = load_data()
    history: list = data.get("history", [])

    # newest first
    filtered = list(reversed(history))

    if team:
        tl = team.lower()
        filtered = [h for h in filtered
                    if tl in h["t1"]["name"].lower() or tl in h["t2"]["name"].lower()]
    if tier:
        filtered = [h for h in filtered if h["tier"] == tier.upper()]
    if event:
        filtered = [h for h in filtered if event.lower() in h["event"].lower()]

    total = len(filtered)
    start = (page - 1) * per_page
    page_data = filtered[start: start + per_page]

    results = []
    for h in page_data:
        t1, t2 = h["t1"], h["t2"]
        winner = t1["name"] if t1["score"] > t2["score"] else t2["name"]
        results.append({
            "date": h["date"],
            "event": h["event"],
            "tier": h["tier"],
            "env": h["env"],
            "grand_final": h.get("grand_final", False),
            "winner": winner,
            "t1": {**t1, "pts_delta": round(t1["pts_after"] - t1["pts_before"], 2)},
            "t2": {**t2, "pts_delta": round(t2["pts_after"] - t2["pts_before"], 2)},
            "url": h.get("url"),
        })

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page),
        "history": results,
    }


# ---------------------------------------------------------------------------
# Simulation endpoint
# ---------------------------------------------------------------------------

class SimRequest(BaseModel):
    team1: str
    team2: str
    tier: str = "A"
    env: str = "LAN"
    grand_final: bool = False
    bo: int = 3   # best of 1, 3, or 5


@app.post("/api/simulate")
def simulate(req: SimRequest):
    data = load_data()
    teams: dict = data.get("teams", {})
    history: list = data.get("history", [])
    provisional: dict = data.get("provisional_teams", {})

    def find(name: str):
        return next(((k, v) for k, v in teams.items() if k.lower() == name.lower()), (None, None))

    name1, r1 = find(req.team1)
    name2, r2 = find(req.team2)

    if r1 is None:
        raise HTTPException(status_code=404, detail=f"Team '{req.team1}' not found")
    if r2 is None:
        raise HTTPException(status_code=404, detail=f"Team '{req.team2}' not found")
    if req.bo not in (1, 3, 5):
        raise HTTPException(status_code=400, detail="bo must be 1, 3, or 5")

    # Form adjustment — exact port of CSRS.py: form_adj = form_score - 50,
    # using the real weighted win/map/comp + streak formula, not a raw win rate.
    def form_adj(team_name: str) -> float:
        form = _calculate_form_at_match_index(team_name, len(history), history)
        if not form:
            return 0.0
        _, score, _ = form
        return round(score - 50, 2)

    fa1 = form_adj(name1)
    fa2 = form_adj(name2)

    # Apply depreciation — ratings decay when teams are inactive
    from datetime import datetime
    date_index = _build_match_date_index(history)
    today = datetime.now()

    def dep_rating(team_name: str, raw_pts: float) -> tuple:
        last = _get_last_match_date(team_name, index=date_index)
        if last is None:
            return raw_pts, 0, 0
        days = (today - last).days
        dep = _calculate_depreciation(raw_pts, days, team_name=team_name)
        return dep, round(raw_pts - dep, 2), days

    r1_dep, r1_dep_loss, r1_days = dep_rating(name1, r1)
    r2_dep, r2_dep_loss, r2_days = dep_rating(name2, r2)

    result = simulate_elo(
        r1_dep, r2_dep,
        tier=req.tier,
        env=req.env,
        grand_final=req.grand_final,
        bo=req.bo,
        form_adj_1=fa1,
        form_adj_2=fa2,
        t1_provisional=name1 in provisional,
        t2_provisional=name2 in provisional,
        t1_provisional_matches=provisional.get(name1, 0),
        t2_provisional_matches=provisional.get(name2, 0),
    )

    # Get current ranks using depreciated ratings
    dep_all = {}
    di_index = _build_match_date_index(history)
    from datetime import datetime as _dt
    _now = _dt.now()
    for n, p in teams.items():
        lm = _get_last_match_date(n, index=di_index)
        di = (_now - lm).days if lm else 0
        dep_all[n] = _calculate_depreciation(p, di, team_name=n)
    ranked = sorted(dep_all.items(), key=lambda x: x[1], reverse=True)
    rank1 = next((i + 1 for i, (n, _) in enumerate(ranked) if n == name1), None)
    rank2 = next((i + 1 for i, (n, _) in enumerate(ranked) if n == name2), None)

    return {
        "team1": {
            "name": name1,
            "points": round(r1_dep, 2),
            "raw_points": round(r1, 2),
            "depreciation_loss": r1_dep_loss,
            "days_inactive": r1_days,
            "rank": rank1,
            "form_adj": fa1,
            "provisional": name1 in provisional,
            "provisional_matches_played": provisional.get(name1, 0),
        },
        "team2": {
            "name": name2,
            "points": round(r2_dep, 2),
            "raw_points": round(r2, 2),
            "depreciation_loss": r2_dep_loss,
            "days_inactive": r2_days,
            "rank": rank2,
            "form_adj": fa2,
            "provisional": name2 in provisional,
            "provisional_matches_played": provisional.get(name2, 0),
        },
        "tier": req.tier,
        "env":  req.env,
        "bo":   req.bo,
        "grand_final": req.grand_final,
        **result,
    }


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/api/analytics/summary")
def analytics_summary():
    data = load_data()
    history: list = data.get("history", [])
    teams: dict = data.get("teams", {})

    tier_counts: dict = {}
    env_counts: dict = {}
    event_counts: dict = {}
    team_wins: dict = {}
    team_losses: dict = {}

    for h in history:
        tier_counts[h["tier"]] = tier_counts.get(h["tier"], 0) + 1
        env_counts[h["env"]] = env_counts.get(h["env"], 0) + 1
        event_counts[h["event"]] = event_counts.get(h["event"], 0) + 1

        t1, t2 = h["t1"], h["t2"]
        if t1["score"] > t2["score"]:
            team_wins[t1["name"]] = team_wins.get(t1["name"], 0) + 1
            team_losses[t2["name"]] = team_losses.get(t2["name"], 0) + 1
        else:
            team_wins[t2["name"]] = team_wins.get(t2["name"], 0) + 1
            team_losses[t1["name"]] = team_losses.get(t1["name"], 0) + 1

    # winrate for teams with >= 5 matches
    winrates = []
    for name in teams:
        w = team_wins.get(name, 0)
        l = team_losses.get(name, 0)
        total = w + l
        if total >= 5:
            winrates.append({"name": name, "wins": w, "losses": l,
                             "winrate": round(w / total * 100, 1)})
    winrates.sort(key=lambda x: x["winrate"], reverse=True)

    top_events = sorted(event_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "total_matches": len(history),
        "total_teams": len(teams),
        "tier_breakdown": tier_counts,
        "env_breakdown": env_counts,
        "top_events": [{"event": e, "matches": c} for e, c in top_events],
        "winrates": winrates[:20],
    }


@app.get("/api/analytics/h2h")
def head_to_head(team1: str = Query(...), team2: str = Query(...)):
    data = load_data()
    history: list = data.get("history", [])

    t1l, t2l = team1.lower(), team2.lower()
    matches = []
    t1_wins = 0
    t2_wins = 0

    for h in history:
        n1, n2 = h["t1"]["name"].lower(), h["t2"]["name"].lower()
        if (t1l in n1 or t1l in n2) and (t2l in n1 or t2l in n2):
            side1 = "t1" if t1l in n1 else "t2"
            side2 = "t2" if side1 == "t1" else "t1"
            me = h[side1]
            opp = h[side2]
            won = me["score"] > opp["score"]
            if won:
                t1_wins += 1
            else:
                t2_wins += 1
            matches.append({
                "date": h["date"],
                "event": h["event"],
                "tier": h["tier"],
                "score": f"{me['score']}-{opp['score']}",
                "winner": me["name"] if won else opp["name"],
                "url": h.get("url"),
            })

    return {
        "team1": team1,
        "team2": team2,
        "total_matches": len(matches),
        "team1_wins": t1_wins,
        "team2_wins": t2_wins,
        "matches": list(reversed(matches)),
    }


# ---------------------------------------------------------------------------
# Elite Teams Over Time — matches CSRS.py display_elite_teams_over_time()
# ---------------------------------------------------------------------------

def _calculate_form_at_match_index(team_name: str, match_index: int, history: list, n: int = 15):
    """
    Exact port of CSRS.py calculate_form_at_match_index (default n=15, matching
    CSRS.py exactly). The `n` parameter is an extension used by get_team() to
    compute a 3-month-windowed form score — CSRS.py itself never varies it.
    Calculates form using only matches strictly before `match_index`,
    so historical transition points never leak future results.
    Returns (grade, score, streak) or None if insufficient data.
    """
    if not history:
        return None

    sliced_history = history[:match_index]

    team_matches = []
    for m in sliced_history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team_name:
            team_matches.append(('t1', m))
        elif t2_name == team_name:
            team_matches.append(('t2', m))

    if len(team_matches) < 3:
        return None

    recent = team_matches[-n:]
    total_weight = 0.0
    win_weighted = 0.0
    map_wins_weighted = 0.0
    map_total_weighted = 0.0
    comp_weighted = 0.0
    streak_chars = []

    for idx, (side, m) in enumerate(recent):
        t = m.get(side, {})
        opp_side = 't2' if side == 't1' else 't1'
        opp = m.get(opp_side, {})

        t_score = t.get('score', 0)
        opp_score = opp.get('score', 0)
        won = t_score > opp_score

        matches_ago = len(recent) - 1 - idx
        exponent = 2.0
        normalized = matches_ago / max(1, len(recent) - 1)
        recency = 1.0 - (normalized ** exponent) * 0.85
        recency = max(0.15, recency)

        win_weighted += (1.0 if won else 0.0) * recency
        map_wins_weighted += t_score * recency
        map_total_weighted += (t_score + opp_score) * recency

        opp_pts = opp.get('pts_before', 500)
        comp_weighted += (opp_pts / DIMINISHING_MAX) * recency

        total_weight += recency
        streak_chars.append('W' if won else 'L')

    if total_weight == 0:
        return None

    win_rate = win_weighted / total_weight
    map_win_rate = map_wins_weighted / map_total_weighted if map_total_weighted > 0 else 0.5
    comp_rate = comp_weighted / total_weight

    def apply_form_compression(value, threshold=FORM_DIMINISHING_THRESHOLD,
                                compression=FORM_DIMINISHING_COMPRESSION):
        if not FORM_DIMINISHING_ENABLED or value <= threshold:
            return value
        gain_above_threshold = value - threshold
        compressed_gain = gain_above_threshold * (1.0 - compression)
        result = threshold + compressed_gain
        return min(1.0, max(threshold, result))

    win_rate_compressed = apply_form_compression(win_rate)
    map_win_rate_compressed = apply_form_compression(map_win_rate)
    comp_rate_compressed = apply_form_compression(comp_rate)

    win_score = win_rate_compressed * FORM_WIN_WEIGHT
    map_score = map_win_rate_compressed * FORM_MAP_WEIGHT
    comp_score = comp_rate_compressed * FORM_COMP_WEIGHT

    base_score = win_score + map_score + comp_score

    streak = ''.join(streak_chars[-n:])
    streak_bonus = 0.0
    consecutive_losses = 0

    if FORM_STREAK_BONUS_ENABLED:
        for result in streak:
            if result == 'W':
                streak_bonus += FORM_STREAK_BONUS_PER_WIN
                streak_bonus = min(FORM_STREAK_BONUS_MAX, streak_bonus)
                consecutive_losses = 0
            else:
                consecutive_losses += 1
                if consecutive_losses >= FORM_STREAK_LOSS_RESET_COUNT:
                    streak_bonus = 0.0
                    consecutive_losses = 0
                else:
                    streak_bonus *= 0.5
        streak_bonus = min(FORM_STREAK_BONUS_MAX, streak_bonus)

    score = base_score + streak_bonus
    score = min(score, 100.0)

    total_form_points = FORM_WIN_WEIGHT + FORM_MAP_WEIGHT + FORM_COMP_WEIGHT
    if score >= total_form_points * 0.85:   grade = 'S'
    elif score >= total_form_points * 0.70: grade = 'A'
    elif score >= total_form_points * 0.55: grade = 'B'
    elif score >= total_form_points * 0.40: grade = 'C'
    else:                                   grade = 'D'

    return grade, round(score, 1), streak


def _calculate_depreciation(current_rating: float, days_inactive: int,
                             team_name: str = None, match_index: int = None,
                             history: list = None) -> float:
    """
    Exact port of CSRS.py calculate_depreciation, adapted to take an explicit
    match_index + history so form is computed via calculate_form_at_match_index
    (no future-data leakage) instead of the global calculate_form.
    """
    if days_inactive <= DEPRECIATION_THRESHOLD:
        return current_rating

    base_decay = (((min(days_inactive, 75) - DEPRECIATION_THRESHOLD) /
                   (75 - DEPRECIATION_THRESHOLD)) ** 2) * 0.25

    form_modifier = 1.0
    if team_name and match_index is not None and history is not None:
        form = _calculate_form_at_match_index(team_name, match_index, history)
        if form:
            form_score = form[1]
            form_modifier = 1.0 - ((form_score - 50) / 250)
            form_modifier = max(FORM_MODIFIER_MIN, min(FORM_MODIFIER_MAX, form_modifier))

    decay_factor = base_decay * form_modifier
    depreciated_rating = current_rating * (1 - decay_factor)

    return max(RATING_FLOOR, depreciated_rating)


@app.get("/api/analytics/elite-over-time")
def elite_over_time(
    start_date: str = Query(..., description="Start date YYYY-MM-DD"),
    end_date:   str = Query(..., description="End date YYYY-MM-DD"),
):
    """
    Returns all teams that reached ELITE_THRESHOLD (850) during the date range,
    with their full rating timeline including starting ratings and depreciation.
    Matches CSRS.py display_elite_teams_over_time() logic exactly.
    """
    from datetime import date as date_type

    data = load_data()
    history: list = data.get("history", [])
    teams: dict   = data.get("teams", {})

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt   = datetime.strptime(end_date,   "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")

    sorted_hist = sorted(
        history,
        key=lambda m: (m.get("date", "") == "N/A", m.get("date", ""))
    )

    # --- Pass 1: find teams that peaked >= ELITE_THRESHOLD in range ---
    team_peaks_in_range: dict = {}
    for m in sorted_hist:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            match_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (start_dt <= match_date <= end_dt):
            continue
        for side in ("t1", "t2"):
            name = m[side]["name"]
            pts  = m[side].get("pts_after")
            if pts is not None:
                if name not in team_peaks_in_range or pts > team_peaks_in_range[name]:
                    team_peaks_in_range[name] = pts

    elite_names = {n for n, p in team_peaks_in_range.items() if p >= ELITE_THRESHOLD}

    # --- Find start_point_date (day before first match in range) ---
    first_match_in_range = None
    for m in sorted_hist:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            match_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if match_date >= start_dt:
            first_match_in_range = m
            first_match_date = match_date
            break

    from datetime import timedelta
    if first_match_in_range:
        start_point_date = (first_match_date - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_point_date = start_date

    # --- Collect starting ratings (pts_before of first match in range) ---
    starting_ratings: dict = {}
    for m in sorted_hist:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            match_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if match_date < start_dt or match_date > end_dt:
            continue
        for side in ("t1", "t2"):
            name = m[side]["name"]
            if name in elite_names and name not in starting_ratings:
                pts_before = m[side].get("pts_before")
                if pts_before is not None:
                    starting_ratings[name] = pts_before

    # --- Carry forward each team's rating from BEFORE the window, so a short
    #     range (e.g. "Last Month") doesn't start a team's line mid-chart at
    #     their first in-range match — it should pick up wherever their
    #     rating actually was the moment start_dt began, exactly like the
    #     "3 months" / "all time" views already show. We scan history once,
    #     in order, tracking each elite team's pts_after and its global index
    #     for every match strictly before start_dt; the last one we see is
    #     their carried-forward state at the start of the window.
    carried_rating: dict = {}   # name -> pts_after just before start_dt
    carried_index:  dict = {}   # name -> global history index of that match
    carried_date:   dict = {}   # name -> date() of that match
    for idx, m in enumerate(history):
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            match_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if match_date >= start_dt:
            continue
        for side in ("t1", "t2"):
            name = m[side]["name"]
            if name not in elite_names:
                continue
            pts = m[side].get("pts_after")
            if pts is not None:
                carried_rating[name] = pts
                carried_index[name] = idx
                carried_date[name] = match_date

    # --- Build sparse match timeline per team, tracking global match_index
    #     (index into `history`) so depreciation can use form-at-that-point ---
    sparse: dict = {name: [] for name in elite_names}  # list of (date, pts, match_index)
    all_dates = {start_point_date}

    for idx, m in enumerate(history):
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            match_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if match_date < start_dt or match_date > end_dt:
            continue
        date_key = date_str[:10]
        all_dates.add(date_key)
        for side in ("t1", "t2"):
            name = m[side]["name"]
            pts  = m[side].get("pts_after")
            if name in elite_names and pts is not None:
                sparse[name].append((date_key, round(pts, 2), idx))

    all_dates.add(end_date)

    # --- Expand each team to day-by-day points with incremental depreciation ---
    series = []
    for name in sorted(elite_names, key=lambda n: teams.get(n, 0), reverse=True):
        matches = sparse.get(name, [])
        if not matches:
            continue

        matches.sort(key=lambda t: t[0])  # ensure chronological

        # match_dates: date -> (pts, match_index)
        match_dates = {d: (p, mi) for d, p, mi in matches}

        first_match_date = datetime.strptime(matches[0][0], "%Y-%m-%d").date()

        if name in carried_rating:
            # Team already had a rating going into this window (from a match
            # before start_dt) — start the line at start_dt itself, carrying
            # forward that rating (with depreciation applied for any gap up
            # to the window) instead of starting mid-chart at the first
            # in-range match.
            start_rating     = carried_rating[name]
            start_point       = start_dt
            last_match_day    = carried_date[name]
            last_match_index  = carried_index[name]
        else:
            # No match before start_dt for this team — fall back to the old
            # behavior: start one day before their first in-range match.
            start_rating      = starting_ratings.get(name, teams.get(name, 1000))
            start_point        = max(start_dt, first_match_date - timedelta(days=1))
            last_match_day     = first_match_date
            last_match_index   = matches[0][2]

        day_points = []
        current_rating    = start_rating

        current_day = start_point
        while current_day <= end_dt:
            date_key = current_day.strftime("%Y-%m-%d")
            is_match_today = date_key in match_dates
            if is_match_today:
                # Port of CSRS.py's "flat-to-diagonal transition marker": a gap of
                # more than 1 day between matches means there was a flat (or
                # depreciating) run leading into this match. Mark the day
                # immediately before it — the last point of that run — so the
                # frontend can draw a circle at the bend, same as the desktop app.
                gap_days = (current_day - last_match_day).days
                if gap_days > 1 and day_points:
                    day_points[-1]["transition"] = True
                current_rating, last_match_index = match_dates[date_key]
                last_match_day = current_day

            days_inactive = (current_day - last_match_day).days
            display_rating = _calculate_depreciation(
                current_rating, days_inactive,
                team_name=name, match_index=last_match_index + 1, history=history
            )

            day_points.append({
                "date":       date_key,
                "pts":        round(display_rating, 2),
                "match":      is_match_today,
                "above":      display_rating >= ELITE_THRESHOLD,
                "deprecated": days_inactive > DEPRECIATION_THRESHOLD,
                "transition": False,
            })
            current_day += timedelta(days=1)

        if not day_points:
            continue

        final_rating   = day_points[-1]["pts"]
        initial_rating = day_points[0]["pts"]
        diff           = final_rating - initial_rating
        peak           = team_peaks_in_range.get(name, current_rating)
        above_thresh   = final_rating >= ELITE_THRESHOLD
        has_depreciation = any(p["deprecated"] for p in day_points)

        current_rank = sorted(teams.items(), key=lambda x: x[1], reverse=True)
        rank = next((i + 1 for i, (n, _) in enumerate(current_rank) if n == name), None)

        series.append({
            "name":             name,
            "points":           day_points,
            "initial_rating":   round(initial_rating, 2),
            "final_rating":     final_rating,
            "peak_in_range":    round(peak, 2),
            "diff":             round(diff, 2),
            "rank":             rank,
            "above_threshold":  above_thresh,
            "has_depreciation": has_depreciation,
        })

    all_dates_sorted = sorted(all_dates | {p["date"] for s in series for p in s["points"]})

    return {
        "start_date":        start_date,
        "end_date":          end_date,
        "start_point_date":  start_point_date,
        "elite_threshold":   ELITE_THRESHOLD,
        "all_dates":         all_dates_sorted,
        "total_teams":       len(series),
        "series":            series,
    }




@app.get("/api/teams")
def list_teams(search: str = Query("")):
    data = load_data()
    teams = list(data.get("teams", {}).keys())
    if search:
        teams = [t for t in teams if search.lower() in t.lower()]
    return {"teams": sorted(teams)}


@app.get("/api/events")
def list_events(
    search: str = Query(""),
    month: str = Query("", description="Filter to events active in YYYY-MM"),
):
    data = load_data()
    history: list = data.get("history", [])

    if month:
        try:
            y, mo = month.split("-")
            month_start = datetime(int(y), int(mo), 1)
            month_end = datetime(int(y) + 1, 1, 1) if int(mo) == 12 else datetime(int(y), int(mo) + 1, 1)
        except Exception:
            raise HTTPException(status_code=400, detail="month must be YYYY-MM")

        def _in_month(date_str):
            if not date_str or date_str == "N/A":
                return False
            try:
                d = _parse_match_date(date_str)
                return month_start <= d < month_end
            except Exception:
                return False

        events_in_month: dict = {}
        for h in history:
            ev = h.get("event") or h.get("event_name")
            if ev and _in_month(h.get("date", "")):
                events_in_month.setdefault(ev, {"match_count": 0, "tier": h.get("tier", "")})
                events_in_month[ev]["match_count"] += 1
        result = [
            {"name": k, "match_count": v["match_count"], "tier": v["tier"]}
            for k, v in sorted(events_in_month.items())
            if not search or search.lower() in k.lower()
        ]
        return {"events": result}

    events = sorted({h["event"] for h in history if h.get("event")})
    if search:
        events = [e for e in events if search.lower() in e.lower()]
    return {"events": events}


@app.get("/api/event-summary")
def get_event_summary(event: str = Query(..., description="Exact event name")):
    data = load_data()
    history: list = data.get("history", [])

    event_matches = [
        m for m in history
        if (m.get("event") or m.get("event_name", "")) == event
    ]
    if not event_matches:
        raise HTTPException(status_code=404, detail=f"No matches found for event '{event}'")

    sorted_matches = sorted(event_matches, key=lambda m: m.get("date", ""))

    tier = sorted_matches[0].get("tier", "N/A")
    env_counts: dict = {}
    for m in sorted_matches:
        env_counts[m.get("env", "UNKNOWN")] = env_counts.get(m.get("env", "UNKNOWN"), 0) + 1

    dates = []
    for m in sorted_matches:
        ds = m.get("date", "")
        if ds and ds != "N/A":
            try:
                dates.append(datetime.strptime(ds[:10], "%Y-%m-%d").date())
            except Exception:
                pass

    start_date = min(dates) if dates else None
    end_date   = max(dates) if dates else None

    team_stats: dict = {}
    for m in sorted_matches:
        for side in ("t1", "t2"):
            td = m.get(side, {})
            name = td.get("name")
            if not name:
                continue
            pts_before = td.get("pts_before")
            pts_after  = td.get("pts_after")
            if pts_before is None or pts_after is None:
                continue
            opp_side = "t2" if side == "t1" else "t1"
            my_score  = td.get("score", 0)
            opp_score = m.get(opp_side, {}).get("score", 0)
            won = my_score > opp_score
            if name not in team_stats:
                team_stats[name] = {
                    "start_rating": float(pts_before),
                    "end_rating":   float(pts_after),
                    "wins": 0, "losses": 0,
                    "maps_won": 0, "maps_lost": 0,
                    "matches": 0,
                }
            s = team_stats[name]
            s["matches"]    += 1
            s["end_rating"]  = float(pts_after)
            s["wins"]       += 1 if won else 0
            s["losses"]     += 0 if won else 1
            s["maps_won"]   += my_score
            s["maps_lost"]  += opp_score

    team_first_gi: dict = {}
    team_last_gi:  dict = {}
    for gi, m in enumerate(history):
        ev = m.get("event") or m.get("event_name", "")
        if ev != event:
            continue
        for side in ("t1", "t2"):
            name = m.get(side, {}).get("name")
            if not name:
                continue
            if name not in team_first_gi:
                team_first_gi[name] = gi
            team_last_gi[name] = gi

    for name in team_stats:
        first_gi = team_first_gi.get(name)
        last_gi  = team_last_gi.get(name)
        form_start = None
        if first_gi is not None:
            f = _calculate_form_at_match_index(name, first_gi, history)
            if f:
                form_start = {"grade": f[0], "score": f[1]}
        form_end = None
        if last_gi is not None:
            f = _calculate_form_at_match_index(name, last_gi + 1, history)
            if f:
                form_end = {"grade": f[0], "score": f[1]}
        team_stats[name]["form_start"]    = form_start
        team_stats[name]["form_end"]      = form_end
        team_stats[name]["rating_change"] = team_stats[name]["end_rating"] - team_stats[name]["start_rating"]
        team_stats[name]["form_change"]   = (
            round(form_end["score"] - form_start["score"], 1)
            if form_start and form_end else None
        )
        team_stats[name]["map_diff"] = team_stats[name]["maps_won"] - team_stats[name]["maps_lost"]

    upsets = []
    for m in sorted_matches:
        t1, t2 = m.get("t1", {}), m.get("t2", {})
        pb1, pb2 = t1.get("pts_before", 0), t2.get("pts_before", 0)
        pa1, pa2 = t1.get("pts_after",  0), t2.get("pts_after",  0)
        s1, s2   = t1.get("score", 0), t2.get("score", 0)
        if not (pb1 and pb2):
            continue
        if s1 > s2 and pb1 < pb2:
            upsets.append({"diff": round(pb2 - pb1, 1), "winner": t1.get("name"), "loser": t2.get("name"),
                           "score": f"{s1}-{s2}", "winner_pts_delta": round(pa1 - pb1, 2), "loser_pts_delta": round(pa2 - pb2, 2)})
        elif s2 > s1 and pb2 < pb1:
            upsets.append({"diff": round(pb1 - pb2, 1), "winner": t2.get("name"), "loser": t1.get("name"),
                           "score": f"{s2}-{s1}", "winner_pts_delta": round(pa2 - pb2, 2), "loser_pts_delta": round(pa1 - pb1, 2)})
    upsets.sort(key=lambda x: x["diff"], reverse=True)

    grand_final = None
    for m in sorted_matches:
        if m.get("grand_final"):
            t1, t2 = m.get("t1", {}), m.get("t2", {})
            s1, s2 = t1.get("score", 0), t2.get("score", 0)
            winner = t1.get("name") if s1 > s2 else t2.get("name")
            loser  = t2.get("name") if s1 > s2 else t1.get("name")
            grand_final = {"winner": winner, "loser": loser, "score": f"{max(s1,s2)}-{min(s1,s2)}"}
            break

    all_changes   = [s["rating_change"] for s in team_stats.values()]
    avg_rating_change = round(sum(all_changes) / len(all_changes), 1) if all_changes else 0
    form_changes  = [s["form_change"] for s in team_stats.values() if s["form_change"] is not None]
    avg_form_change = round(sum(form_changes) / len(form_changes), 1) if form_changes else None

    highest_team   = max(team_stats.items(), key=lambda x: x[1]["end_rating"], default=None)
    most_maps_team = max(team_stats.items(), key=lambda x: x[1]["maps_won"] + x[1]["maps_lost"], default=None)

    gainers        = sorted([(n, s) for n, s in team_stats.items() if s["rating_change"] > 0],  key=lambda x: x[1]["rating_change"], reverse=True)
    losers         = sorted([(n, s) for n, s in team_stats.items() if s["rating_change"] < 0],  key=lambda x: x[1]["rating_change"])
    form_improvers = sorted([(n, s) for n, s in team_stats.items() if (s["form_change"] or 0) > 0], key=lambda x: x[1]["form_change"], reverse=True)
    form_decliners = sorted([(n, s) for n, s in team_stats.items() if (s["form_change"] or 0) < 0], key=lambda x: x[1]["form_change"])

    def _ts(name, s):
        return {
            "name": name,
            "start_rating": round(s["start_rating"]),
            "end_rating":   round(s["end_rating"]),
            "rating_change": round(s["rating_change"], 1),
            "wins": s["wins"], "losses": s["losses"],
            "maps_won": s["maps_won"], "maps_lost": s["maps_lost"],
            "map_diff": s["map_diff"],
            "form_start": s["form_start"], "form_end": s["form_end"],
            "form_change": s["form_change"],
        }

    return {
        "event": event,
        "tier": tier,
        "environments": env_counts,
        "match_count": len(sorted_matches),
        "team_count": len(team_stats),
        "start_date": str(start_date) if start_date else None,
        "end_date":   str(end_date)   if end_date   else None,
        "grand_final": grand_final,
        "gainers":        [_ts(n, s) for n, s in gainers[:5]],
        "losers":         [_ts(n, s) for n, s in losers[:5]],
        "form_improvers": [_ts(n, s) for n, s in form_improvers[:5]],
        "form_decliners": [_ts(n, s) for n, s in form_decliners[:5]],
        "upsets":         upsets[:3],
        "all_teams":      [_ts(n, s) for n, s in sorted(team_stats.items(), key=lambda x: x[1]["end_rating"], reverse=True)],
        "stats": {
            "highest_rated_team": highest_team[0] if highest_team else None,
            "highest_rated_pts":  round(highest_team[1]["end_rating"]) if highest_team else None,
            "most_maps_team":     most_maps_team[0] if most_maps_team else None,
            "most_maps_count":    (most_maps_team[1]["maps_won"] + most_maps_team[1]["maps_lost"]) if most_maps_team else None,
            "most_maps_matches":  most_maps_team[1]["matches"] if most_maps_team else None,
            "avg_rating_change":  avg_rating_change,
            "avg_form_change":    avg_form_change,
        },
    }


# ---------------------------------------------------------------------------
# Home screen
# ---------------------------------------------------------------------------

def _parse_match_date(date_str: str):
    clean = date_str.replace(" UTC", "").strip()
    try:
        return datetime.strptime(clean, "%Y-%m-%d %H:%M")
    except ValueError:
        return datetime.strptime(clean[:10], "%Y-%m-%d")


@app.get("/api/home/months")
def home_months():
    """
    Distinct YYYY-MM values for which match history exists, newest first.
    Powers the Month Summary page's month picker.
    """
    data = load_data()
    history: list = data.get("history", [])

    months: set = set()
    for m in history:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            d = _parse_match_date(date_str)
        except Exception:
            continue
        months.add(f"{d.year:04d}-{d.month:02d}")

    return {"months": sorted(months, reverse=True)}


@app.get("/api/home")
def home(
    end: str = Query("", description="Optional YYYY-MM-DD — when set, win/loss streak tiles are computed as of this date instead of live. Every other tile on this endpoint is unaffected and always reflects the current live state."),
    start: str = Query("", description="Optional YYYY-MM-DD — when set together with `end`, replaces the default rolling 30-day window with the explicit [start, end] range (used by Month Summary). `end` is also treated as the 'as of' point in time for rank/rating snapshots in this case, instead of live `now`."),
):
    """
    Aggregated data for the Home screen — all tiles below are scoped to the
    last 30 days unless noted otherwise, computed fresh per-request except
    where noted (peak/best-ever-rank reuses the existing cached replay).

    When `start` and `end` are both supplied (Month Summary), the window
    becomes [start, end] instead of the default rolling 30 days, and `end`
    is used as the "as of" point in time for current rank / rating
    snapshots instead of live `now` — so a historical month reflects
    standings as they actually were at the end of that month.
    """
    from datetime import timedelta

    data = load_data()
    history: list = data.get("history", [])
    teams: dict = data.get("teams", {})

    today = _parse_match_date(end + " 23:59") if (start and end) else datetime.now()
    cutoff_30d = _parse_match_date(start + " 00:00") if (start and end) else today - timedelta(days=30)
    streak_as_of = _parse_match_date(end + " 23:59") if end else None

    recent: list = []
    for m in history:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            d = _parse_match_date(date_str)
        except Exception:
            continue
        if d >= cutoff_30d and d <= today:
            recent.append((d, m))
    recent.sort(key=lambda x: x[0])  # chronological, oldest first

    date_index = _build_match_date_index(history)

    # --- Header stat strip (all-time, not 30d-scoped — totals are totals) ---
    total_matches = len(history)
    total_teams = len(teams)

    # --- #1 ranked team + 30d rating sparkline + form ---
    team_display: dict = {}
    for name, pts in teams.items():
        last = _get_last_match_date(name, index=date_index)
        if last is None:
            team_display[name] = pts
            continue
        days_inactive = (today - last).days
        team_display[name] = _calculate_depreciation(pts, days_inactive, team_name=name)
    ranked_now = sorted(teams.items(), key=lambda x: team_display[x[0]], reverse=True)
    current_rank = {name: i + 1 for i, (name, _pts) in enumerate(ranked_now)}

    top_team = None
    if ranked_now:
        top_name, _ = ranked_now[0]
        spark = [
            round(m[side]["pts_after"], 2)
            for d, m in recent
            for side in ("t1", "t2")
            if m[side]["name"] == top_name
        ]
        form_3m = _calculate_form_at_match_index(top_name, len(history), history)
        top_team = {
            "name": top_name,
            "points": round(team_display[top_name], 2),
            "sparkline_30d": spark,
            "form_grade": form_3m[0] if form_3m else None,
            "form_score": round(form_3m[1], 1) if form_3m else None,
        }

    # --- Featured Results: 5 most recent matches, tier in {S+, S, A, B, C} ---
    # (D = below cutoff, R = deprecated regional tier, both excluded)
    allowed_tiers = {"S+", "S", "A", "B", "C"}
    featured_results = []
    for m in reversed(history):  # newest first, full history (not 30d-capped)
        if m.get("tier") not in allowed_tiers:
            continue
        t1, t2 = m["t1"], m["t2"]
        winner = t1["name"] if t1["score"] > t2["score"] else t2["name"]
        featured_results.append({
            "date": m["date"],
            "event": m["event"],
            "tier": m["tier"],
            "t1": {"name": t1["name"], "score": t1["score"]},
            "t2": {"name": t2["name"], "score": t2["score"]},
            "winner": winner,
        })
        if len(featured_results) >= 5:
            break

    # --- Hot Teams: top win rate, last 30d, min 5 matches, restricted to current top-30 rank ---
    top_30_names = {name for name, _r in current_rank.items() if current_rank[name] <= 30}

    # Matches played in the last 30 days, per team — the shared "enough
    # recent activity" gate for every 30d-window Home tile below (Hot/Cold
    # Teams, Top Rating Increase, Highest/Lowest Rating Change, Highest/Lowest
    # Form Change, Most Positions Gained/Lost). A team with only 1-2 matches
    # in the window can swing wildly and isn't a meaningful "standout".
    MIN_MATCHES_30D = 3
    wins_30d: dict = {}
    losses_30d: dict = {}
    for _d, m in recent:
        t1, t2 = m["t1"], m["t2"]
        if t1["score"] > t2["score"]:
            wins_30d[t1["name"]] = wins_30d.get(t1["name"], 0) + 1
            losses_30d[t2["name"]] = losses_30d.get(t2["name"], 0) + 1
        else:
            wins_30d[t2["name"]] = wins_30d.get(t2["name"], 0) + 1
            losses_30d[t1["name"]] = losses_30d.get(t1["name"], 0) + 1
    matches_30d: dict = {
        name: wins_30d.get(name, 0) + losses_30d.get(name, 0)
        for name in set(wins_30d) | set(losses_30d)
    }

    # Global index of each team's match positions in `history` (chronological).
    # Used below for the #1 Form Team's 30d form sparkline, and further down
    # for the Highest/Lowest Form Change tiles and win/loss streak tiles.
    team_match_indices: dict = {}
    for gi, m in enumerate(history):
        for side in ("t1", "t2"):
            team_match_indices.setdefault(m[side]["name"], []).append(gi)

    # --- #1 Form Team: highest 3-month-style form score among current top-30 ---
    top_form_team = None
    best_form_score = None
    for name in top_30_names:
        form = _calculate_form_at_match_index(name, len(history), history)
        if not form:
            continue
        grade, score, _streak = form
        if best_form_score is None or score > best_form_score:
            best_form_score = score
            # 30d form sparkline: form score as of each of this team's
            # matches in the last 30 days (mirrors the rating sparkline,
            # which uses pts_after at each match in the same window).
            form_spark = []
            for gi in team_match_indices.get(name, []):
                m = history[gi]
                d_str = m.get("date", "")
                if not d_str or d_str == "N/A":
                    continue
                try:
                    d = _parse_match_date(d_str)
                except Exception:
                    continue
                if d < cutoff_30d:
                    continue
                f = _calculate_form_at_match_index(name, gi + 1, history)
                if f:
                    form_spark.append(round(f[1], 1))
            top_form_team = {
                "name": name,
                "form_grade": grade,
                "form_score": round(score, 1),
                "sparkline_30d": form_spark,
            }

    # --- Top 5 Rating Increase: 30d depreciated-rating delta, restricted to top-30 ---
    # Mirrors the same "rank 30 days ago" raw-points snapshot used by the
    # "most positions gained" tile below, just computed earlier so both can
    # reuse it without duplicating the walk over history.
    running_raw_points_30d_ago_for_rating: dict = {}
    for m in history:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            d = _parse_match_date(date_str)
        except Exception:
            continue
        if d > cutoff_30d:
            break
        for side in ("t1", "t2"):
            running_raw_points_30d_ago_for_rating[m[side]["name"]] = m[side]["pts_after"]

    rating_increase = []
    rating_change_all = []  # signed deltas, top-30 teams w/ >= MIN_MATCHES_30D matches — feeds Standouts tiles below
    for name in top_30_names:
        if name not in running_raw_points_30d_ago_for_rating:
            continue  # too new / no snapshot 30 days ago
        if matches_30d.get(name, 0) < MIN_MATCHES_30D:
            continue  # not enough recent activity for a 30d swing to be meaningful
        before = _apply_depreciation(
            name, running_raw_points_30d_ago_for_rating[name],
            match_date=cutoff_30d, index=date_index,
        )
        after = team_display[name]
        delta = round(after - before, 1)
        rating_change_all.append({"name": name, "delta": delta, "_after": after})
        if delta > 0:
            rating_increase.append({"name": name, "delta": delta, "rank": current_rank[name]})
    rating_increase.sort(key=lambda x: x["delta"], reverse=True)
    top_rating_increase = rating_increase[:5]

    # --- Top 5 Rating Decrease: inverse of the above, feeds Cold Teams ---
    rating_decrease = [
        {"name": rc["name"], "delta": rc["delta"], "rank": current_rank[rc["name"]]}
        for rc in rating_change_all
        if rc["delta"] < 0
    ]
    rating_decrease.sort(key=lambda x: x["delta"])
    top_rating_decrease = rating_decrease[:5]

    hot_teams = []
    for name in top_30_names:
        w = wins_30d.get(name, 0)
        l = losses_30d.get(name, 0)
        total = w + l
        if total >= MIN_MATCHES_30D:
            hot_teams.append({
                "name": name, "wins": w, "losses": l,
                "winrate": round(w / total * 100, 1),
                "matches_played": total,
                "rank": current_rank[name],
            })
    # Tie-break: same win rate -> most matches played in the last 30 days wins.
    hot_teams.sort(key=lambda x: (x["winrate"], x["matches_played"]), reverse=True)
    hot_teams = hot_teams[:5]

    # --- Cold Teams: inverse of Hot Teams — lowest win rate, same activity floor ---
    cold_teams = []
    for name in top_30_names:
        w = wins_30d.get(name, 0)
        l = losses_30d.get(name, 0)
        total = w + l
        if total >= MIN_MATCHES_30D:
            cold_teams.append({
                "name": name, "wins": w, "losses": l,
                "winrate": round(w / total * 100, 1),
                "matches_played": total,
                "rank": current_rank[name],
            })
    # Tie-break: same win rate -> most matches played in the last 30 days wins
    # (more matches at a low win rate is a "colder" run than just one or two).
    cold_teams.sort(key=lambda x: (x["winrate"], -x["matches_played"]))
    cold_teams = cold_teams[:5]

    # --- Tile: Highest rating change (30d) ---
    # Same start-of-30d-to-now delta used for "Top 5 Rating Increase" in Hot
    # Teams — picks the team with the single biggest true increase. Tie-break:
    # same delta -> the team with the higher current rating wins.
    tile_rating_change = None
    for rc in rating_change_all:
        delta = rc["delta"]
        is_better = False
        if tile_rating_change is None:
            is_better = True
        elif delta > tile_rating_change["delta"]:
            is_better = True
        elif delta == tile_rating_change["delta"] and rc["_after"] > tile_rating_change["_after"]:
            is_better = True
        if is_better:
            tile_rating_change = {"name": rc["name"], "delta": delta, "_after": rc["_after"]}
    if tile_rating_change:
        tile_rating_change.pop("_after", None)

    # --- Tile: Lowest rating change (30d) — the inverse of the tile above.
    # Picks the single biggest *decrease* specifically (not just biggest
    # absolute swing), so a team that fell off a cliff always wins this slot
    # even if some other team's gain was numerically larger.
    tile_rating_change_low = None
    for rc in rating_change_all:
        delta = rc["delta"]
        is_better = False
        if tile_rating_change_low is None:
            is_better = True
        elif delta < tile_rating_change_low["delta"]:
            is_better = True
        elif delta == tile_rating_change_low["delta"] and rc["_after"] > tile_rating_change_low["_after"]:
            is_better = True
        if is_better:
            tile_rating_change_low = {"name": rc["name"], "delta": delta, "_after": rc["_after"]}
    if tile_rating_change_low:
        tile_rating_change_low.pop("_after", None)

    # --- Tile: Biggest rating-difference upset (30d) — lower pts_before side wins ---
    # Tie-break: same gap -> the upset where the losing (higher-rated) team
    # had the higher rating wins — i.e. a bigger name falling counts more.
    tile_upset = None
    for _d, m in recent:
        t1, t2 = m["t1"], m["t2"]
        winner, loser = (t1, t2) if t1["score"] > t2["score"] else (t2, t1)
        if winner["pts_before"] < loser["pts_before"]:
            gap = loser["pts_before"] - winner["pts_before"]
            is_better = False
            if tile_upset is None:
                is_better = True
            elif gap > tile_upset["gap"]:
                is_better = True
            elif gap == tile_upset["gap"] and loser["pts_before"] > tile_upset["_loser_pts_before"]:
                is_better = True
            if is_better:
                tile_upset = {
                    "winner": winner["name"], "loser": loser["name"],
                    "gap": round(gap, 2), "date": m["date"], "event": m["event"],
                    "_loser_pts_before": loser["pts_before"],
                }
    if tile_upset:
        tile_upset.pop("_loser_pts_before", None)

    # --- Tile: Highest form-score change (30d) ---
    # Compares each team's current form score against their form score as of
    # their last match before the 30-day cutoff. Teams need enough match
    # history on both sides of the cutoff for the comparison to be meaningful.
    tile_form_change = None
    tile_form_change_low = None
    for name in top_30_names:
        if matches_30d.get(name, 0) < MIN_MATCHES_30D:
            continue  # not enough recent activity for a 30d form swing to be meaningful
        indices = team_match_indices.get(name, [])
        idx_before_cutoff = None
        for gi in indices:
            d_str = history[gi].get("date", "")
            if not d_str or d_str == "N/A":
                continue
            try:
                d = _parse_match_date(d_str)
            except Exception:
                continue
            if d <= cutoff_30d:
                idx_before_cutoff = gi + 1  # form calc is exclusive of this index
        if idx_before_cutoff is None:
            continue  # team has no match history before the cutoff

        form_before = _calculate_form_at_match_index(name, idx_before_cutoff, history)
        form_now = _calculate_form_at_match_index(name, len(history), history)
        if not form_before or not form_now:
            continue

        delta = round(form_now[1] - form_before[1], 1)
        is_better = False
        if tile_form_change is None:
            is_better = True
        elif delta > tile_form_change["delta"]:
            is_better = True
        elif delta == tile_form_change["delta"] and form_now[1] > tile_form_change["_form_now"]:
            is_better = True
        if is_better:
            tile_form_change = {
                "name": name, "delta": delta,
                "form_grade": form_now[0],
                "_form_now": form_now[1],
            }

        is_better_low = False
        if tile_form_change_low is None:
            is_better_low = True
        elif delta < tile_form_change_low["delta"]:
            is_better_low = True
        elif delta == tile_form_change_low["delta"] and form_now[1] > tile_form_change_low["_form_now"]:
            is_better_low = True
        if is_better_low:
            tile_form_change_low = {
                "name": name, "delta": delta,
                "form_grade": form_now[0],
                "_form_now": form_now[1],
            }
    if tile_form_change:
        tile_form_change.pop("_form_now", None)
    if tile_form_change_low:
        tile_form_change_low.pop("_form_now", None)

    # --- Tiles: Longest current win streak / loss streak ---
    # Walks each team's full match history backwards from their most recent
    # match (not bounded to the 30d window — a streak can run longer than
    # that), counting consecutive identical results until it breaks.
    # When `end` is supplied (Month Summary), "most recent match" means most
    # recent as of that date, not literally right now — so a historical
    # month shows the streak that was actually current at that point in
    # time, not whatever streak happens to be running today.
    # Tie-break: same streak length -> the team with the higher current
    # (depreciated) rating wins.
    tile_win_streak = None
    tile_loss_streak = None
    for name in top_30_names:
        indices = team_match_indices.get(name, [])
        if streak_as_of is not None:
            indices = [
                gi for gi in indices
                if history[gi].get("date") and history[gi]["date"] != "N/A"
                and _parse_match_date(history[gi]["date"]) <= streak_as_of
            ]
        if not indices:
            continue
        streak_len = 0
        streak_result = None
        for gi in reversed(indices):
            m = history[gi]
            won = m["t1"]["score"] > m["t2"]["score"] if m["t1"]["name"] == name \
                else m["t2"]["score"] > m["t1"]["score"]
            result = "W" if won else "L"
            if streak_result is None:
                streak_result = result
                streak_len = 1
            elif result == streak_result:
                streak_len += 1
            else:
                break
        if streak_result is None:
            continue
        rating = team_display[name]

        if streak_result == "W":
            is_better = False
            if tile_win_streak is None:
                is_better = True
            elif streak_len > tile_win_streak["streak"]:
                is_better = True
            elif streak_len == tile_win_streak["streak"] and rating > tile_win_streak["_rating"]:
                is_better = True
            if is_better:
                tile_win_streak = {"name": name, "streak": streak_len, "_rating": rating}
        else:
            is_better = False
            if tile_loss_streak is None:
                is_better = True
            elif streak_len > tile_loss_streak["streak"]:
                is_better = True
            elif streak_len == tile_loss_streak["streak"] and rating > tile_loss_streak["_rating"]:
                is_better = True
            if is_better:
                tile_loss_streak = {"name": name, "streak": streak_len, "_rating": rating}
    if tile_win_streak:
        tile_win_streak.pop("_rating", None)
    if tile_loss_streak:
        tile_loss_streak.pop("_rating", None)

    # --- Tile: Most positions gained (rank 30 days ago vs rank now) ---
    # Teams with no recorded points 30 days ago (too new) are excluded —
    # there's no valid "before" rank to compare against.
    running_raw_points_30d_ago: dict = {}
    for m in history:
        date_str = m.get("date", "")
        if not date_str or date_str == "N/A":
            continue
        try:
            d = _parse_match_date(date_str)
        except Exception:
            continue
        if d > cutoff_30d:
            break
        for side in ("t1", "t2"):
            running_raw_points_30d_ago[m[side]["name"]] = m[side]["pts_after"]

    depreciated_30d_ago: dict = {
        name: _apply_depreciation(name, pts, match_date=cutoff_30d, index=date_index)
        for name, pts in running_raw_points_30d_ago.items()
    }
    ranked_30d_ago = sorted(depreciated_30d_ago.items(), key=lambda x: x[1], reverse=True)
    rank_30d_ago = {name: i + 1 for i, (name, _pts) in enumerate(ranked_30d_ago)}

    # Tie-break: same positions gained -> the team with the higher current
    # (depreciated) rating wins.
    tile_positions_gained = None
    best_gain = None
    best_gain_rating = None
    # Tie-break: same positions lost -> the team with the higher current
    # (depreciated) rating wins (i.e. the bigger name slipping counts more).
    tile_positions_lost = None
    worst_drop = None
    worst_drop_rating = None
    for name in teams:
        if name not in rank_30d_ago:
            continue
        if matches_30d.get(name, 0) < MIN_MATCHES_30D:
            continue  # rank moved purely from other teams' decay, not from playing
        gained = rank_30d_ago[name] - current_rank[name]  # positive = moved up
        rating = team_display[name]

        # Gained: only counts if they ended up inside the top 30 — climbing
        # from #300 to #250 isn't a "standout", climbing into contention is.
        if current_rank[name] <= 30:
            is_better = False
            if best_gain is None:
                is_better = True
            elif gained > best_gain:
                is_better = True
            elif gained == best_gain and rating > best_gain_rating:
                is_better = True
            if is_better:
                best_gain = gained
                best_gain_rating = rating
                tile_positions_gained = {
                    "name": name, "positions_gained": gained,
                    "rank_30d_ago": rank_30d_ago[name], "rank_now": current_rank[name],
                }

        # Lost: only counts if they started inside the top 30 — falling from
        # #300 to #350 isn't a "standout", falling out of contention is.
        if rank_30d_ago[name] <= 30:
            is_worse = False
            if worst_drop is None:
                is_worse = True
            elif gained < worst_drop:
                is_worse = True
            elif gained == worst_drop and rating > worst_drop_rating:
                is_worse = True
            if is_worse:
                worst_drop = gained
                worst_drop_rating = rating
                tile_positions_lost = {
                    "name": name, "positions_lost": -gained,
                    "rank_30d_ago": rank_30d_ago[name], "rank_now": current_rank[name],
                }

    # --- Active event spotlight: no grand_final played yet for that event ---
    event_has_gf: dict = {}
    event_last_match: dict = {}
    for m in history:
        e = m["event"]
        if m.get("grand_final"):
            event_has_gf[e] = True
        event_last_match[e] = m["date"]  # history is chronological; last write wins
    active_events = [e for e in event_has_gf.keys() | event_last_match.keys() if not event_has_gf.get(e)]
    # most recently active first
    active_events.sort(key=lambda e: event_last_match.get(e, ""), reverse=True)

    return {
        "total_matches": total_matches,
        "total_teams": total_teams,
        "top_team": top_team,
        "top_form_team": top_form_team,
        "featured_results": featured_results,
        "hot_teams": hot_teams,
        "cold_teams": cold_teams,
        "top_rating_increase": top_rating_increase,
        "top_rating_decrease": top_rating_decrease,
        "tiles": {
            "highest_rating_change": tile_rating_change,
            "lowest_rating_change": tile_rating_change_low,
            "highest_form_change": tile_form_change,
            "lowest_form_change": tile_form_change_low,
            "biggest_upset": tile_upset,
            "most_positions_gained": tile_positions_gained,
            "most_positions_lost": tile_positions_lost,
            "longest_win_streak": tile_win_streak,
            "longest_loss_streak": tile_loss_streak,
        },
        "active_events": active_events[:5],
    }


# ---------------------------------------------------------------------------
# Meta / health
# ---------------------------------------------------------------------------

@app.get("/api/meta")
def meta():
    data = load_data()
    history = data.get("history", [])
    last_match = history[-1]["date"] if history else None
    return {
        "version": data.get("version", 1),
        "total_teams": len(data.get("teams", {})),
        "total_matches": len(history),
        "last_match": last_match,
        "data_file": str(DATA_FILE),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")