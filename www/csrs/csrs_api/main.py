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
):
    data = load_data()
    teams: dict = data.get("teams", {})
    peaks: dict = data.get("peaks", {})
    provisional: dict = data.get("provisional_teams", {})
    history: list = data.get("history", [])

    # Build sparkline data from last 30 days of per-team ratings.
    from collections import defaultdict
    from datetime import timedelta

    team_spark: dict = defaultdict(list)
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    for entry in history:
        date_str = entry.get("date", "")
        if date_str[:10] < cutoff:
            continue
        for side in ("t1", "t2"):
            team_name = entry[side]["name"]
            team_spark[team_name].append(round(entry[side]["pts_after"], 2))

    from datetime import datetime

    # Build date index for O(log n) last-match lookups
    date_index = _build_match_date_index(history)
    today = datetime.now()

    # Compute depreciated display ratings
    team_display: dict = {}
    for name, pts in teams.items():
        last = _get_last_match_date(name, index=date_index)
        if last is None:
            team_display[name] = pts
            continue
        days_inactive = (today - last).days
        team_display[name] = _calculate_depreciation(pts, days_inactive, team_name=name)

    # Sort by depreciated rating so rank reflects real standing
    ranked = sorted(teams.items(), key=lambda x: team_display[x[0]], reverse=True)

    results = []
    for rank, (name, pts) in enumerate(ranked, 1):
        if search and search.lower() not in name.lower():
            continue
        dep_pts = team_display[name]
        dep_loss = round(pts - dep_pts, 2)
        last_match = _get_last_match_date(name, index=date_index)
        days_inactive = (today - last_match).days if last_match else 0
        peak_info = peaks.get(name, {})
        spark = team_spark.get(name, [])
        results.append({
            "rank": rank,
            "name": name,
            "points": round(dep_pts, 2),
            "raw_points": round(pts, 2),
            "depreciation_loss": dep_loss if dep_loss > 0 else 0,
            "days_inactive": days_inactive,
            "peak_points": round(peak_info.get("points", pts), 2),
            "peak_date": peak_info.get("date"),
            "peak_rank": peak_info.get("rank"),
            "provisional": name in provisional,
            "matches_until_ranked": provisional.get(name, 0) if name in provisional else None,
            "sparkline": spark,
        })

    return {"total": len(results), "rankings": results[:limit]}


# ---------------------------------------------------------------------------
# Team detail + rating history
# ---------------------------------------------------------------------------

@app.get("/api/team/{team_name}")
def get_team(team_name: str):
    data = load_data()
    teams: dict = data.get("teams", {})
    history: list = data.get("history", [])
    peaks: dict = data.get("peaks", {})
    aliases: dict = data.get("aliases", {})

    # resolve alias
    resolved = aliases.get(team_name.lower(), team_name)
    # case-insensitive match
    matched = next((t for t in teams if t.lower() == resolved.lower()), None)
    if not matched:
        raise HTTPException(status_code=404, detail=f"Team '{team_name}' not found")

    pts = teams[matched]
    ranked = sorted(teams.items(), key=lambda x: x[1], reverse=True)

    # Apply depreciation for display
    from datetime import datetime
    date_index = _build_match_date_index(history)
    today = datetime.now()
    last_match_dt = _get_last_match_date(matched, index=date_index)
    days_inactive = (today - last_match_dt).days if last_match_dt else 0
    dep_pts = _calculate_depreciation(pts, days_inactive, team_name=matched)
    dep_loss = round(pts - dep_pts, 2)

    # Rank by depreciated ratings
    team_dep = {}
    for n, p in teams.items():
        lm = _get_last_match_date(n, index=date_index)
        di = (today - lm).days if lm else 0
        team_dep[n] = _calculate_depreciation(p, di, team_name=n)
    ranked_dep = sorted(team_dep.items(), key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (n, _) in enumerate(ranked_dep) if n == matched), None)

    # build rating history timeline
    timeline = []
    for entry in history:
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
            "date": entry["date"],
            "event": entry["event"],
            "opponent": opp["name"],
            "score": f"{me['score']}-{opp['score']}",
            "won": won,
            "pts_before": round(me["pts_before"], 2),
            "pts_after": round(me["pts_after"], 2),
            "pts_delta": round(me["pts_after"] - me["pts_before"], 2),
            "tier": entry["tier"],
            "env": entry["env"],
            "url": entry.get("url"),
        })

    peak_info = peaks.get(matched, {})

    return {
        "name": matched,
        "points": round(dep_pts, 2),
        "raw_points": round(pts, 2),
        "depreciation_loss": dep_loss if dep_loss > 0 else 0,
        "days_inactive": days_inactive,
        "rank": rank,
        "peak": peak_info,
        "total_matches": len(timeline),
        "wins": sum(1 for t in timeline if t["won"]),
        "losses": sum(1 for t in timeline if not t["won"]),
        "timeline": timeline,
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
        },
        "team2": {
            "name": name2,
            "points": round(r2_dep, 2),
            "raw_points": round(r2, 2),
            "depreciation_loss": r2_dep_loss,
            "days_inactive": r2_days,
            "rank": rank2,
            "form_adj": fa2,
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

def _calculate_form_at_match_index(team_name: str, match_index: int, history: list):
    """
    Exact port of CSRS.py calculate_form_at_match_index.
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

    recent = team_matches[-15:]
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
        normalized = matches_ago / 14
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

    streak = ''.join(streak_chars[-15:])
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
        start_rating = starting_ratings.get(name, teams.get(name, 1000))

        # match_dates: date -> (pts, match_index)
        match_dates = {d: (p, mi) for d, p, mi in matches}

        first_match_date = datetime.strptime(matches[0][0], "%Y-%m-%d").date()
        start_point = max(start_dt, first_match_date - timedelta(days=1))

        day_points = []
        current_rating    = start_rating
        last_match_day     = first_match_date
        last_match_index   = matches[0][2]

        current_day = start_point
        while current_day <= end_dt:
            date_key = current_day.strftime("%Y-%m-%d")
            if date_key in match_dates:
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
                "match":      date_key in match_dates,
                "above":      display_rating >= ELITE_THRESHOLD,
                "deprecated": days_inactive > DEPRECIATION_THRESHOLD,
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
    