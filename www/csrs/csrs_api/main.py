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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
# Elo simulation helpers (mirrors CSRS.py logic)
# ---------------------------------------------------------------------------

TIER_MULTIPLIERS = {"S": 2.0, "A": 1.5, "B": 1.0, "C": 0.6, "D": 0.3, "R": 0.1}
ENV_MULTIPLIERS  = {"LAN": 1.2, "STAGE": 1.1, "STUDIO": 1.0, "ONLINE": 0.85}
BASE_K = 32


def expected_score(ra: float, rb: float) -> float:
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def simulate_elo(r1: float, r2: float, tier: str, env: str,
                 grand_final: bool = False) -> dict:
    k = BASE_K * TIER_MULTIPLIERS.get(tier, 1.0) * ENV_MULTIPLIERS.get(env, 1.0)
    if grand_final:
        k *= 1.25
    e1 = expected_score(r1, r2)
    e2 = 1 - e1

    # win outcome
    d1_win = round(k * (1 - e1), 2)
    d2_win = round(k * (0 - e2), 2)

    # loss outcome
    d1_loss = round(k * (0 - e1), 2)
    d2_loss = round(k * (1 - e2), 2)

    win_prob_t1 = round(e1 * 100, 1)

    return {
        "win_probability_t1": win_prob_t1,
        "win_probability_t2": round(100 - win_prob_t1, 1),
        "if_t1_wins":  {"t1_delta": d1_win,  "t2_delta": d2_win},
        "if_t2_wins":  {"t1_delta": d1_loss, "t2_delta": d2_loss},
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

    ranked = sorted(teams.items(), key=lambda x: x[1], reverse=True)

    results = []
    for rank, (name, pts) in enumerate(ranked, 1):
        if search and search.lower() not in name.lower():
            continue
        peak_info = peaks.get(name, {})
        results.append({
            "rank": rank,
            "name": name,
            "points": round(pts, 2),
            "peak_points": round(peak_info.get("points", pts), 2),
            "peak_date": peak_info.get("date"),
            "peak_rank": peak_info.get("rank"),
            "provisional": name in provisional,
            "matches_until_ranked": provisional.get(name, 0) if name in provisional else None,
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


@app.post("/api/simulate")
def simulate(req: SimRequest):
    data = load_data()
    teams: dict = data.get("teams", {})

    def find(name: str):
        return next((v for k, v in teams.items() if k.lower() == name.lower()), None)

    r1 = find(req.team1)
    r2 = find(req.team2)

    if r1 is None:
        raise HTTPException(status_code=404, detail=f"Team '{req.team1}' not found")
    if r2 is None:
        raise HTTPException(status_code=404, detail=f"Team '{req.team2}' not found")

    result = simulate_elo(r1, r2, req.tier, req.env, req.grand_final)
    return {
        "team1": {"name": req.team1, "points": round(r1, 2)},
        "team2": {"name": req.team2, "points": round(r2, 2)},
        "tier": req.tier,
        "env": req.env,
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
# Teams list (for autocomplete)
# ---------------------------------------------------------------------------

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
