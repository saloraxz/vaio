# -*- coding: utf-8 -*-
"""
CSRS Admin API — FastAPI backend (port 8001, Tailscale-only)
Protected by HTTP Basic Auth + CSRF tokens.
Handles: point adjustments, team management, import triggering, log viewing.
"""

import base64
import zlib
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_FILE      = Path(os.environ.get("CSRS_DATA_FILE",  "data.save"))
BATCH_LOG_FILE = Path(os.environ.get("BATCH_LOG_FILE",  "batch_import.log"))
FRONTEND_DIR   = Path(os.environ.get("ADMIN_FRONTEND_DIR", "admin_frontend"))

ADMIN_USERNAME  = os.environ.get("ADMIN_USERNAME",   "admin")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD",   "changeme")
SECRET_KEY      = os.environ.get("ADMIN_SECRET_KEY", secrets.token_hex(32))

# In-memory CSRF token store: token -> expiry timestamp
_csrf_tokens: dict[str, float] = {}
CSRF_TTL = 1800  # 30 minutes

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="CSRS Admin API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """HTTP Basic Auth — constant time comparison to prevent timing attacks."""
    correct_user = secrets.compare_digest(
        credentials.username.encode(), ADMIN_USERNAME.encode()
    )
    correct_pass = secrets.compare_digest(
        credentials.password.encode(), ADMIN_PASSWORD.encode()
    )
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_csrf(request: Request):
    """Validate CSRF token from X-CSRF-Token header."""
    token = request.headers.get("X-CSRF-Token", "")
    now = time.time()
    # Clean expired tokens
    expired = [t for t, exp in _csrf_tokens.items() if exp < now]
    for t in expired:
        del _csrf_tokens[t]
    if not token or token not in _csrf_tokens:
        raise HTTPException(status_code=403, detail="Invalid or expired CSRF token")
    # Refresh expiry on use
    _csrf_tokens[token] = now + CSRF_TTL


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if not DATA_FILE.exists():
        raise HTTPException(status_code=503, detail=f"data.save not found at {DATA_FILE}")
    try:
        raw = DATA_FILE.read_bytes()
        decoded = base64.b64decode(raw)
        return json.loads(zlib.decompress(decoded))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read data.save: {e}")


def save_data(data: dict) -> None:
    """Re-encode and atomically write data.save."""
    try:
        encoded = base64.b64encode(
            zlib.compress(json.dumps(data, ensure_ascii=False).encode())
        ).decode()
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(encoded, encoding="utf-8")
        tmp.replace(DATA_FILE)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write data.save: {e}")


def find_team(data: dict, name: str) -> Optional[str]:
    """Case-insensitive team lookup."""
    teams = data.get("teams", {})
    return next((t for t in teams if t.lower() == name.lower()), None)


# ---------------------------------------------------------------------------
# CSRF token endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/csrf-token")
def get_csrf_token(username: str = Depends(require_auth)):
    """Issue a fresh CSRF token. Frontend calls this on page load."""
    token = secrets.token_hex(32)
    _csrf_tokens[token] = time.time() + CSRF_TTL
    return {"csrf_token": token}


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------

class AdjustRequest(BaseModel):
    team: str
    amount: float
    reason: str = ""


@app.get("/admin/adjustments")
def list_adjustments(username: str = Depends(require_auth)):
    data = load_data()
    adjustments = data.get("adjustments", [])
    return {"adjustments": list(reversed(adjustments))}


@app.post("/admin/adjust")
def apply_adjustment(
    req: AdjustRequest,
    username: str = Depends(require_auth),
    _csrf=Depends(require_csrf),
):
    data = load_data()
    matched = find_team(data, req.team)
    if not matched:
        raise HTTPException(status_code=404, detail=f"Team '{req.team}' not found")

    old_pts = data["teams"][matched]
    new_pts = max(0.0, old_pts + req.amount)  # floor at 0

    data["teams"][matched] = new_pts

    adj_record = {
        "team":       matched,
        "date":       datetime.now().strftime("%Y-%m-%d %H:%M"),
        "old_points": old_pts,
        "new_points": new_pts,
        "adjustment": req.amount,
        "reason":     req.reason,
    }
    data.setdefault("adjustments", []).append(adj_record)

    save_data(data)
    return {
        "team":       matched,
        "old_points": round(old_pts, 2),
        "new_points": round(new_pts, 2),
        "adjustment": req.amount,
    }


# ---------------------------------------------------------------------------
# Team management
# ---------------------------------------------------------------------------

class AddTeamRequest(BaseModel):
    name: str
    points: float


class DeleteTeamRequest(BaseModel):
    name: str


@app.post("/admin/team/add")
def add_team(
    req: AddTeamRequest,
    username: str = Depends(require_auth),
    _csrf=Depends(require_csrf),
):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Team name cannot be empty")
    data = load_data()
    if find_team(data, req.name):
        raise HTTPException(status_code=409, detail=f"Team '{req.name}' already exists")
    data["teams"][req.name] = max(0.0, req.points)
    # Mark as provisional (needs 3 matches before leaving provisional status)
    data.setdefault("provisional_teams", {})[req.name] = 0
    save_data(data)
    return {"name": req.name, "points": req.points, "provisional": True}


@app.post("/admin/team/delete")
def delete_team(
    req: DeleteTeamRequest,
    username: str = Depends(require_auth),
    _csrf=Depends(require_csrf),
):
    data = load_data()
    matched = find_team(data, req.name)
    if not matched:
        raise HTTPException(status_code=404, detail=f"Team '{req.name}' not found")

    match_count = sum(
        1 for m in data.get("history", [])
        if m.get("t1", {}).get("name") == matched
        or m.get("t2", {}).get("name") == matched
    )

    del data["teams"][matched]
    data.get("provisional_teams", {}).pop(matched, None)
    data.get("peaks", {}).pop(matched, None)

    save_data(data)
    return {"deleted": matched, "had_matches": match_count}


# ---------------------------------------------------------------------------
# Import control
# ---------------------------------------------------------------------------

_last_import_time: Optional[str] = None
_last_import_result: Optional[str] = None


@app.post("/admin/import/trigger")
def trigger_import(
    username: str = Depends(require_auth),
    _csrf=Depends(require_csrf),
):
    """
    Force the actual csrs-daemon container to run an import cycle right now,
    instead of waiting for its 30-minute interval. Works by dropping a marker
    file on the shared /data volume — the daemon polls for this every 5
    seconds while sleeping and wakes immediately when it appears.
    This signals the REAL daemon loop (so the next scheduled run resets from
    now), rather than spawning a separate one-off process.
    """
    global _last_import_time, _last_import_result

    trigger_file = DATA_FILE.parent / "force_import.trigger"

    try:
        trigger_file.write_text(datetime.now().isoformat(), encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not write trigger file: {e}")

    _last_import_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _last_import_result = "running"
    return {"status": "triggered", "triggered_at": _last_import_time}


@app.get("/admin/import/status")
def import_status(username: str = Depends(require_auth)):
    """
    Status of the most recent import, inferred from batch_import.log.
    Since the daemon now runs the actual import (not a spawned subprocess),
    "running" is detected by checking whether the log's last line indicates
    an in-progress cycle (started but not yet completed).
    """
    global _last_import_result

    log_lines = []
    running = False
    if BATCH_LOG_FILE.exists():
        try:
            lines = BATCH_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            log_lines = lines[-30:]
            # Detect "running" state: last "===" block opened but not yet closed
            for line in reversed(lines):
                if "Auto-import complete" in line:
                    running = False
                    _last_import_result = "success" if "0 failed" in line or "failed" not in line else "failed"
                    break
                if "Auto-import started" in line:
                    running = True
                    break
        except Exception:
            pass

    trigger_file = DATA_FILE.parent / "force_import.trigger"
    trigger_pending = trigger_file.exists()

    return {
        "running":         running,
        "trigger_pending": trigger_pending,
        "last_run":        _last_import_time,
        "last_result":     _last_import_result,
        "log_tail":        log_lines,
    }


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get("/admin/logs")
def get_logs(
    lines: int = 50,
    username: str = Depends(require_auth),
):
    if not BATCH_LOG_FILE.exists():
        return {"lines": []}
    try:
        all_lines = BATCH_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": all_lines[-lines:]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@app.get("/admin/meta")
def admin_meta(username: str = Depends(require_auth)):
    data = load_data()
    return {
        "total_teams":   len(data.get("teams", {})),
        "total_matches": len(data.get("history", [])),
        "adjustments":   len(data.get("adjustments", [])),
        "provisional":   len(data.get("provisional_teams", {})),
        "data_file":     str(DATA_FILE),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Serve admin frontend
# ---------------------------------------------------------------------------

ADMIN_INDEX_FILE = FRONTEND_DIR / "admin_index.html"


@app.get("/", response_class=HTMLResponse)
def serve_admin_index():
    if not ADMIN_INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="admin_index.html not found")
    return ADMIN_INDEX_FILE.read_text(encoding="utf-8")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="admin-static")
