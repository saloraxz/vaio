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
) -> dict:
    """
    Full simulation matching CSRS.py simulate_match() output.
    Returns win probabilities and point deltas for every possible scoreline.
    """
    wins_needed = (bo // 2) + 1

    # Effective ratings for win probability
    eff1 = r1 + form_adj_1
    eff2 = r2 + form_adj_2
    p_map = _win_probability(eff1, eff2)
    series_prob_t1 = _series_win_prob(p_map, bo)

    # All possible scorelines
    scorelines = []
    for loser_maps in range(wins_needed):
        # t1 wins
        map_diff = wins_needed - loser_maps  # maps won by winner
        new_r1 = _calculate_points(r1, r2, 1, map_diff, tier, env, grand_final,
                                   form_adj_1, form_adj_2, t2_provisional)
        new_r2 = _calculate_points(r2, r1, 0, map_diff, tier, env, grand_final,
                                   form_adj_2, form_adj_1, t1_provisional)
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

    ranked = sorted(teams.items(), key=lambda x: x[1], reverse=True)

    results = []
    for rank, (name, pts) in enumerate(ranked, 1):
        if search and search.lower() not in name.lower():
            continue
        peak_info = peaks.get(name, {})
        spark = team_spark.get(name, [])
        results.append({
            "rank": rank,
            "name": name,
            "points": round(pts, 2),
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
    rank = next((i + 1 for i, (n, _) in enumerate(ranked) if n == matched), None)

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
        "points": round(pts, 2),
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

    # Calculate form adjustments from recent history (last 15 matches)
    def form_adj(team_name: str) -> float:
        matches = [
            m for m in history
            if m.get("t1", {}).get("name") == team_name
            or m.get("t2", {}).get("name") == team_name
        ][-15:]
        if len(matches) < 3:
            return 0.0
        wins = sum(
            1 for m in matches
            if (m["t1"]["name"] == team_name and m["t1"]["score"] > m["t2"]["score"])
            or (m["t2"]["name"] == team_name and m["t2"]["score"] > m["t1"]["score"])
        )
        win_rate = wins / len(matches)
        # Scale -50 to +50 form adjustment (centred at 50% win rate)
        return round((win_rate - 0.5) * 100, 2)

    fa1 = form_adj(name1)
    fa2 = form_adj(name2)

    result = simulate_elo(
        r1, r2,
        tier=req.tier,
        env=req.env,
        grand_final=req.grand_final,
        bo=req.bo,
        form_adj_1=fa1,
        form_adj_2=fa2,
        t1_provisional=name1 in provisional,
        t2_provisional=name2 in provisional,
    )

    # Get current ranks
    ranked = sorted(teams.items(), key=lambda x: x[1], reverse=True)
    rank1 = next((i + 1 for i, (n, _) in enumerate(ranked) if n == name1), None)
    rank2 = next((i + 1 for i, (n, _) in enumerate(ranked) if n == name2), None)

    return {
        "team1": {"name": name1, "points": round(r1, 2), "rank": rank1, "form_adj": fa1},
        "team2": {"name": name2, "points": round(r2, 2), "rank": rank2, "form_adj": fa2},
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
# Elite Teams Over Time
# ---------------------------------------------------------------------------

@app.get("/api/analytics/elite-over-time")
def elite_over_time(
    top: int = Query(10, ge=2, le=30, description="Number of top teams to include"),
):
    data = load_data()
    teams: dict = data.get("teams", {})
    history: list = data.get("history", [])

    # Get top N teams by current rating
    ranked = sorted(teams.items(), key=lambda x: x[1], reverse=True)[:top]
    top_names = {name for name, _ in ranked}

    # Build per-team timeline from history
    from collections import defaultdict
    team_points: dict = defaultdict(list)

    for entry in history:
        for side in ("t1", "t2"):
            name = entry[side]["name"]
            if name in top_names:
                team_points[name].append({
                    "date":  entry["date"].split(" ")[0],
                    "pts":   round(entry[side]["pts_after"], 2),
                })

    # Build a unified sorted date list across all teams
    all_dates = sorted(set(
        p["date"]
        for points in team_points.values()
        for p in points
    ))

    # For each team, produce a sparse series (only dates they played)
    series = []
    for name, _ in ranked:
        points = team_points.get(name, [])
        series.append({
            "name":   name,
            "points": [{"date": p["date"], "pts": p["pts"]} for p in points],
        })

    return {
        "dates":  all_dates,
        "series": series,
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