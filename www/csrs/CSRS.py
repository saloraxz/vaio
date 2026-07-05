# -*- coding: utf-8 -*-
"""
CSRS.py - Counter-Strike Rating System
A comprehensive Elo-based rating system for esports teams.
Includes match importing, history management, and analytics.
"""

# =============================================================================
# === IMPORTS ===
# =============================================================================

import json
import base64
import zlib
import sys
import os
import re
import subprocess
import logging
import bisect
import time
from collections import deque
from datetime import datetime, timedelta
from tkinter import Tk
from typing import Dict, List, Tuple, Optional, Any

# =============================================================================
# === DEPENDENCY MANAGEMENT ===
# =============================================================================

def check_and_install_dependencies() -> bool:
    """
    Check for required dependencies and attempt to install them if missing.
    Returns True if all dependencies are available, False otherwise.
    """
    import subprocess
    import sys
    
    missing_deps = []
    
    # Check playwright
    try:
        from playwright.sync_api import sync_playwright
        print("[OK] playwright is installed")
    except ImportError:
        missing_deps.append("playwright")
        print("[!] playwright is NOT installed")
    
    if not missing_deps:
        return True
    
    print(f"\n{'='*60}")
    print("  MISSING DEPENDENCIES DETECTED")
    print(f"{'='*60}")
    print(f"\n  Missing: {', '.join(missing_deps)}")
    print("\n  Attempting automatic installation...")
    print(f"{'='*60}\n")
    
    # Attempt to install
    for dep in missing_deps:
        print(f"  Installing {dep}...")
        try:
            # Use subprocess to call pip
            subprocess.check_call([sys.executable, "-m", "pip", "install", dep, "--quiet"])
            print(f"  [OK] {dep} installed successfully")
            
            # Special handling for playwright - needs browser installation
            if dep == "playwright":
                print(f"  Installing playwright browsers (this may take a minute)...")
                subprocess.check_call([sys.executable, "-m", "playwright", "install"], timeout=120)
                print(f"  [OK] Playwright browsers installed")
                
        except subprocess.CalledProcessError as e:
            print(f"  [ERROR] Failed to install {dep}")
            print(f"  Error: {e}")
            return False
        except subprocess.TimeoutExpired:
            print(f"  [ERROR] Installation timed out for {dep}")
            return False
        except Exception as e:
            print(f"  [ERROR] Unexpected error installing {dep}: {e}")
            return False
    
    print(f"\n{'='*60}")
    print("  ALL DEPENDENCIES INSTALLED SUCCESSFULLY")
    print(f"{'='*60}")
    print("\n  Please restart the program to continue.")
    print(f"{'='*60}\n")
    
    input("Press Enter to exit...")
    return False

# =============================================================================
# === LOGGING SETUP ===
# =============================================================================

_LOG_DIR     = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "normal")
_ERR_LOG_DIR = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "errors")
_FAIL_LOG_DIR= os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "fails")
os.makedirs(_LOG_DIR,      exist_ok=True)
os.makedirs(_ERR_LOG_DIR,  exist_ok=True)
os.makedirs(_FAIL_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(_LOG_DIR, "csrs.log"), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# === EXCEPTIONS ===
# =============================================================================

class MenuException(Exception):
    """Custom exception to force return to main menu."""
    pass

# =============================================================================
# === GLOBAL TYPE DECLARATIONS (For Linter) ===
# =============================================================================
# These tell the IDE these variables exist even if defined later

teams: Dict[str, float] = {}
history: List[Dict[str, Any]] = []
peak_ratings: Dict[str, Dict[str, Any]] = {}
event_tiers: Dict[str, str] = {}
provisional_teams: Dict[str, int] = {}  # team_name -> matches played (removed at 3)
adjustments: List[Dict[str, Any]] = []
aliases: Dict[str, str] = {}
unsaved_changes: bool = False
STARTING_TEAMS: Dict[str, float] = {}
SAVE_VERSION: int = 1
SAVE_FILE: str = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "main", "data.save")
ERROR_LOG: str = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "errors", "csrs_error.log")

# =============================================================================
# === CONFIGURATION ===
# =============================================================================
# Change these values to customize your rating system
# Or create a config.json file to override defaults

DEFAULT_CONFIG = {
    "RATING_CAP": 12930,
    "RATING_FLOOR": 0,
    "K_WIN": 33,
    "K_LOSS": 22,
    "ELITE_THRESHOLD": 850,
    "FORM_WIN_WEIGHT": 42.5,
    "FORM_MAP_WEIGHT": 42.5,
    "FORM_COMP_WEIGHT": 15.0,
    "FORM_STREAK_BONUS_ENABLED": True,
    "FORM_STREAK_BONUS_MAX": 10.0,
    "FORM_STREAK_BONUS_PER_WIN": 2,
    "FORM_STREAK_LOSS_RESET_COUNT": 2,
    "FORM_MODIFIER_MIN": 0.5,
    "FORM_MODIFIER_MAX": 1.5,
    "DIMINISHING_RETURNS_ENABLED": True,
    "DIMINISHING_THRESHOLD": 1000,
    "DIMINISHING_MAX": 1050,
    "DIMINISHING_K_WIN_MIN_PERCENT": 0,
    "DIMINISHING_K_LOSS_MIN_PERCENT": 1,
    "FORM_DIMINISHING_ENABLED": True,
    "FORM_DIMINISHING_THRESHOLD": 0.85,
    "FORM_DIMINISHING_COMPRESSION": 0.67,
    "MISMATCH_PENALTY_ENABLED": True,
    "MISMATCH_DECAY_PERCENT": 1,
    "MISMATCH_ZERO_POINT_PERCENT": 0.70,
    "MISMATCH_MAX_PENALTY_PERCENT": 0.66,
    "MISMATCH_MAX_PENALTY_VALUE": -0.75,
    "PITY_POINTS_ENABLED": True,
    "PITY_THRESHOLD_PERCENT": 0.75,
    "PITY_MAX_PERCENT": 0.65,
    "PITY_MAX_POINTS": 9,
    "PITY_MIN_POINTS": 6,
    "MISMATCH_REFERENCE_RATING": 1000,
    # === SLIDING GAP THRESHOLDS (mirrored between mismatch and pity) ===
    # At/above SLIDING_THRESHOLD_ANCHOR_HIGH elo, thresholds behave exactly as their normal
    # values above (MISMATCH_ZERO_POINT_PERCENT=0.70, PITY_THRESHOLD_PERCENT=0.75). At/below
    # SLIDING_THRESHOLD_ANCHOR_LOW elo, both thresholds ease toward their *_LOW_ELO value —
    # requiring a bigger relative gap before either the mismatch penalty or the pity bonus
    # triggers at all. This is mirrored deliberately: at low elo a modest absolute Elo gap is
    # already a big relative gap (see MISMATCH_REFERENCE_RATING above), so both the
    # win-punishing and loss-softening mechanisms are dialed down together rather than one
    # side of the squeeze getting relief while the other doesn't.
    "SLIDING_THRESHOLD_ANCHOR_LOW": 400,
    "MISMATCH_ZERO_POINT_PERCENT_LOW_ELO": 0.50,
    "PITY_THRESHOLD_PERCENT_LOW_ELO": 0.50,
    "DEPRECIATION_THRESHOLD": 14,
    "INACTIVE_ARCHIVE_DAYS": 180,
}

# Load Configuration
CONFIG = DEFAULT_CONFIG.copy()
_CONFIG_FILE = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "data", "config.json")
if os.path.exists(_CONFIG_FILE):
    try:
        with open(_CONFIG_FILE, "r", encoding='utf-8') as f:
            user_config = json.load(f)
            CONFIG.update(user_config)
        logger.info("Loaded custom config.json")
    except Exception as e:
        logger.error(f"Error loading config.json, using defaults: {e}")

# Apply Config to Globals
RATING_CAP = CONFIG.get("RATING_CAP", DEFAULT_CONFIG["RATING_CAP"])
RATING_FLOOR = CONFIG.get("RATING_FLOOR", DEFAULT_CONFIG["RATING_FLOOR"])
K_WIN = CONFIG.get("K_WIN", DEFAULT_CONFIG["K_WIN"])
K_LOSS = CONFIG.get("K_LOSS", DEFAULT_CONFIG["K_LOSS"])

# === PROVISIONAL RATING SYSTEM ===
PROVISIONAL_STARTING_RATING = 400       # Starting CSRS for unranked teams
PROVISIONAL_MATCH_THRESHOLD = 3        # Matches needed to become established
PROVISIONAL_K_FACTORS = {1: 3.0, 2: 2.5, 3: 2.0}  # K multiplier per match number
PROVISIONAL_OPP_DIFF_CAP = 200         # Max rating diff used vs provisional opponent

# === S / S+ TIER AUTO-DETECTION ===
# S tier: named events (IEM Cologne, IEM Krakow) or any Major, by event name
S_TIER_EVENT_NAME_PATTERNS = ['iem cologne', 'iem krakow', 'iem kraków', 'major']
# S+ tier: a Major held at a venue that has also hosted an S-tier event (e.g. Cologne, Krakow)
MAJOR_PATTERN = 'major'
S_TIER_VENUE_KEYWORDS = ['cologne', 'köln', 'koln', 'krakow', 'kraków', 'lanxess', 'tauron']

ELITE_THRESHOLD = CONFIG.get("ELITE_THRESHOLD", DEFAULT_CONFIG["ELITE_THRESHOLD"])
FORM_WIN_WEIGHT = CONFIG.get("FORM_WIN_WEIGHT", DEFAULT_CONFIG["FORM_WIN_WEIGHT"])
FORM_MAP_WEIGHT = CONFIG.get("FORM_MAP_WEIGHT", DEFAULT_CONFIG["FORM_MAP_WEIGHT"])
FORM_COMP_WEIGHT = CONFIG.get("FORM_COMP_WEIGHT", DEFAULT_CONFIG["FORM_COMP_WEIGHT"])
FORM_STREAK_BONUS_ENABLED = CONFIG.get("FORM_STREAK_BONUS_ENABLED", DEFAULT_CONFIG["FORM_STREAK_BONUS_ENABLED"])
FORM_STREAK_BONUS_MAX = CONFIG.get("FORM_STREAK_BONUS_MAX", DEFAULT_CONFIG["FORM_STREAK_BONUS_MAX"])
FORM_STREAK_BONUS_PER_WIN = CONFIG.get("FORM_STREAK_BONUS_PER_WIN", DEFAULT_CONFIG["FORM_STREAK_BONUS_PER_WIN"])
FORM_STREAK_LOSS_RESET_COUNT = CONFIG.get("FORM_STREAK_LOSS_RESET_COUNT", DEFAULT_CONFIG["FORM_STREAK_LOSS_RESET_COUNT"])
FORM_MODIFIER_MIN = CONFIG.get("FORM_MODIFIER_MIN", DEFAULT_CONFIG["FORM_MODIFIER_MIN"])
FORM_MODIFIER_MAX = CONFIG.get("FORM_MODIFIER_MAX", DEFAULT_CONFIG["FORM_MODIFIER_MAX"])
DIMINISHING_RETURNS_ENABLED = CONFIG.get("DIMINISHING_RETURNS_ENABLED", DEFAULT_CONFIG["DIMINISHING_RETURNS_ENABLED"])
DIMINISHING_THRESHOLD = CONFIG.get("DIMINISHING_THRESHOLD", DEFAULT_CONFIG["DIMINISHING_THRESHOLD"])
DIMINISHING_MAX = CONFIG.get("DIMINISHING_MAX", DEFAULT_CONFIG["DIMINISHING_MAX"])
DIMINISHING_K_WIN_MIN_PERCENT = CONFIG.get("DIMINISHING_K_WIN_MIN_PERCENT", DEFAULT_CONFIG["DIMINISHING_K_WIN_MIN_PERCENT"])
DIMINISHING_K_LOSS_MIN_PERCENT = CONFIG.get("DIMINISHING_K_LOSS_MIN_PERCENT", DEFAULT_CONFIG["DIMINISHING_K_LOSS_MIN_PERCENT"])
FORM_DIMINISHING_ENABLED = CONFIG.get("FORM_DIMINISHING_ENABLED", DEFAULT_CONFIG["FORM_DIMINISHING_ENABLED"])
FORM_DIMINISHING_THRESHOLD = CONFIG.get("FORM_DIMINISHING_THRESHOLD", DEFAULT_CONFIG["FORM_DIMINISHING_THRESHOLD"])
FORM_DIMINISHING_COMPRESSION = CONFIG.get("FORM_DIMINISHING_COMPRESSION", DEFAULT_CONFIG["FORM_DIMINISHING_COMPRESSION"])
MISMATCH_PENALTY_ENABLED = CONFIG.get("MISMATCH_PENALTY_ENABLED", DEFAULT_CONFIG["MISMATCH_PENALTY_ENABLED"])
MISMATCH_DECAY_PERCENT = CONFIG.get("MISMATCH_DECAY_PERCENT", DEFAULT_CONFIG["MISMATCH_DECAY_PERCENT"])
MISMATCH_ZERO_POINT_PERCENT = CONFIG.get("MISMATCH_ZERO_POINT_PERCENT", DEFAULT_CONFIG["MISMATCH_ZERO_POINT_PERCENT"])
MISMATCH_MAX_PENALTY_PERCENT = CONFIG.get("MISMATCH_MAX_PENALTY_PERCENT", DEFAULT_CONFIG["MISMATCH_MAX_PENALTY_PERCENT"])
MISMATCH_MAX_PENALTY_VALUE = CONFIG.get("MISMATCH_MAX_PENALTY_VALUE", DEFAULT_CONFIG["MISMATCH_MAX_PENALTY_VALUE"])
PITY_POINTS_ENABLED = CONFIG.get("PITY_POINTS_ENABLED", DEFAULT_CONFIG["PITY_POINTS_ENABLED"])
PITY_THRESHOLD_PERCENT = CONFIG.get("PITY_THRESHOLD_PERCENT", DEFAULT_CONFIG["PITY_THRESHOLD_PERCENT"])
PITY_MAX_PERCENT = CONFIG.get("PITY_MAX_PERCENT", DEFAULT_CONFIG["PITY_MAX_PERCENT"])
PITY_MAX_POINTS = CONFIG.get("PITY_MAX_POINTS", DEFAULT_CONFIG["PITY_MAX_POINTS"])
PITY_MIN_POINTS = CONFIG.get("PITY_MIN_POINTS", DEFAULT_CONFIG["PITY_MIN_POINTS"])

# Fixed reference rating used to convert mismatch/pity gaps into a "percent" figure.
# BUG THIS FIXES: mismatch/pity used to divide by the team's OWN live rating
# (opponent_points/team_points and team_points/opponent_points). That makes the exact same
# absolute Elo-point gap look like a much bigger "mismatch" for a low-rated team than for a
# high-rated one (e.g. a 100pt gap is 20% of a 500-rated team's rating but only ~7% of a
# 1500-rated team's), which one-sidedly punished wins for low-rated teams (mismatch decay
# starts at ANY weaker opponent) without giving them equivalent extra protection on losses
# (pity only starts past a 25% gap) — low-rated clusters drifted downward even at a break-even
# win rate. Using a single fixed reference rating instead means the same Elo-point gap is
# treated the same way everywhere on the ladder.
MISMATCH_REFERENCE_RATING = CONFIG.get("MISMATCH_REFERENCE_RATING", DEFAULT_CONFIG["MISMATCH_REFERENCE_RATING"])

# === SLIDING GAP THRESHOLDS (mirrored between mismatch and pity) ===
# MISMATCH_ZERO_POINT_PERCENT (0.70) and PITY_THRESHOLD_PERCENT (0.75) are the "how big a
# relative gap is needed" thresholds. Rather than leaving them fixed, they slide between
# their normal value (used at/above SLIDING_THRESHOLD_ANCHOR_HIGH, which reuses
# MISMATCH_REFERENCE_RATING) and a more lenient *_LOW_ELO value (used at/below
# SLIDING_THRESHOLD_ANCHOR_LOW) — requiring a bigger relative gap before either mechanism
# triggers at all when the higher-rated team in the match is itself low elo. Both slide the
# same direction on purpose: at low elo a modest absolute gap already reads as a big relative
# gap, so both the win-punishing (mismatch) and loss-softening (pity) mechanisms get muted
# together rather than one easing up while the other stays strict.
SLIDING_THRESHOLD_ANCHOR_LOW = CONFIG.get("SLIDING_THRESHOLD_ANCHOR_LOW", DEFAULT_CONFIG["SLIDING_THRESHOLD_ANCHOR_LOW"])
SLIDING_THRESHOLD_ANCHOR_HIGH = MISMATCH_REFERENCE_RATING
MISMATCH_ZERO_POINT_PERCENT_LOW_ELO = CONFIG.get("MISMATCH_ZERO_POINT_PERCENT_LOW_ELO", DEFAULT_CONFIG["MISMATCH_ZERO_POINT_PERCENT_LOW_ELO"])
PITY_THRESHOLD_PERCENT_LOW_ELO = CONFIG.get("PITY_THRESHOLD_PERCENT_LOW_ELO", DEFAULT_CONFIG["PITY_THRESHOLD_PERCENT_LOW_ELO"])

def _sliding_threshold(higher_elo: float, strict_value: float, lenient_value: float) -> float:
    """
    Interpolate a gap-threshold between its lenient value (at/below
    SLIDING_THRESHOLD_ANCHOR_LOW elo) and its strict/normal value (at/above
    SLIDING_THRESHOLD_ANCHOR_HIGH elo), based on the higher-rated team in the match.
    """
    span = SLIDING_THRESHOLD_ANCHOR_HIGH - SLIDING_THRESHOLD_ANCHOR_LOW
    if span <= 0:
        return strict_value
    position = (higher_elo - SLIDING_THRESHOLD_ANCHOR_LOW) / span
    position = max(0.0, min(1.0, position))
    return lenient_value + position * (strict_value - lenient_value)

DEPRECIATION_THRESHOLD = CONFIG.get("DEPRECIATION_THRESHOLD", DEFAULT_CONFIG.get("DEPRECIATION_THRESHOLD", 14))

# Preserve the original spacing between "where the penalty starts" and "where it caps out"
# as both thresholds slide together (see _sliding_threshold above).
_MISMATCH_ZERO_TO_MAXPENALTY_GAP = MISMATCH_ZERO_POINT_PERCENT - MISMATCH_MAX_PENALTY_PERCENT
_PITY_THRESHOLD_TO_MAX_GAP = PITY_THRESHOLD_PERCENT - PITY_MAX_PERCENT

# === INACTIVE-TEAM ARCHIVING (VRS-style rolling window, layered ON TOP of depreciation) ===
# Depreciation (above) discounts an inactive team's rating by up to 25%, then holds — it
# never fully clears them out. INACTIVE_ARCHIVE_DAYS is a separate, much longer threshold
# (default 180 days, mirroring Valve's own ~6-month VRS window) past which a team is
# considered "archived": hidden from the default rankings/graphs view so the visible list
# doesn't grow forever, but never deleted, still fully depreciated as normal, and it
# automatically reappears the instant it plays a new match. See is_team_archived().
INACTIVE_ARCHIVE_DAYS = CONFIG.get("INACTIVE_ARCHIVE_DAYS", DEFAULT_CONFIG.get("INACTIVE_ARCHIVE_DAYS", 180))

# Pre-baked lookup table for the base depreciation decay curve. The curve
# is a pure function of days_inactive (clamped to [0, 75]) and the fixed
# DEPRECIATION_THRESHOLD/DEPRECIATION_CAP_DAYS constants above, so it's
# computed once here instead of re-evaluating the quadratic expression on
# every calculate_depreciation() call (which happens for both teams on
# every match during resimulation, plus every get_sorted_rankings call).
_DEPRECIATION_CAP_DAYS = 75
_BASE_DECAY_TABLE = [0.0] * (_DEPRECIATION_CAP_DAYS + 1)
for _d in range(_DEPRECIATION_CAP_DAYS + 1):
    if _d > DEPRECIATION_THRESHOLD:
        _BASE_DECAY_TABLE[_d] = (((_d - DEPRECIATION_THRESHOLD) / (_DEPRECIATION_CAP_DAYS - DEPRECIATION_THRESHOLD)) ** 2) * 0.25
del _d


def _base_decay_for(days_inactive: int) -> float:
    """O(1) lookup for the base depreciation decay curve (days_inactive clamped to 75)."""
    return _BASE_DECAY_TABLE[min(max(days_inactive, 0), _DEPRECIATION_CAP_DAYS)]


SAVE_VERSION = 1
SAVE_FILE = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "main", "data.save")
ERROR_LOG = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "errors", "csrs_error.log")
# =============================================================================

SAVE_VERSION = 1  # Increment this if data structure changes
SAVE_FILE = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "main", "data.save")
ERROR_LOG = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "errors", "csrs_error.log")

# Legacy file names - used only for one-time migration on first run
_LEGACY_DEFAULT  = "default.txt"
_LEGACY_ALIASES  = "aliases.json"
_LEGACY_HISTORY  = "match_history.json"

# Initial Team Ratings (Starting Points)
# Empty — all ratings are established through match history via VRS lookup.
teams = {}

# Baseline points used for resimulation — built at registration time and
# persisted in data.save. Empty on a fresh start.
STARTING_TEAMS = {}

# Dynamic Data Structures (Loaded from save)
# These are populated when loading save files and updated during runtime
aliases = {}              # Maps team nicknames/abbreviations to full team names
history = []              # List of all match records with scores, dates, ratings
peak_ratings = {}         # team_name -> {"points": float, "date": str, "rank": int}
event_tiers = {}          # event_name -> tier (S+/S/A/B/C/D) for auto-assignment
adjustments = []          # List of manual point adjustment records for audit trail
unsaved_changes = False   # Tracks if data has been modified since last manual save
total_imports = 0         # Persistent running count of imports (drives tiered backups)

# Tiered backup schedule: maps tier number -> import interval.
# On each import, only the HIGHEST tier whose interval divides total_imports
# is written — so import #20 (divisible by 5, 10, and 20) only writes to
# backup_3 (interval 20), not backup_1 or backup_2.
BACKUP_TIERS = {
    1: 5,
    2: 10,
    3: 20,
    4: 50,
    5: 100,
}

def mark_unsaved():
    """Flag that data has been modified and needs saving."""
    global unsaved_changes
    unsaved_changes = True

def error_log(message: str) -> None:
    """Append critical errors to a log file for debugging."""
    logger.error(message)

def _build_save_code(t, a, h):
    """Encode teams, aliases, history, peaks, event_tiers, adjustments, provisional_teams, total_imports into base64."""
    try:
        payload = json.dumps({
            "version": SAVE_VERSION,
            "teams": t,
            "aliases": a,
            "history": h,
            "peaks": peak_ratings,
            "event_tiers": event_tiers,
            "adjustments": adjustments,
            "provisional_teams": provisional_teams,
            "total_imports": total_imports,
        })
        return base64.b64encode(zlib.compress(payload.encode())).decode()
    except Exception as e:
        error_log(f"Failed to build save code: {e}")
        return None

def _parse_save_code(raw_code):
    """Decode a save code. Returns (teams, aliases, history, peaks, event_tiers, adjustments, provisional_teams, total_imports) or raises."""
    try:
        decoded = zlib.decompress(base64.b64decode(raw_code)).decode()
        data = json.loads(decoded)
        
        if data.get("version", 0) != SAVE_VERSION:
            print(f">>> WARNING: Save version mismatch (File: {data.get('version', 0)}, Expected: {SAVE_VERSION})")
        
        if isinstance(data, dict) and "teams" not in data:
            return data, {}, [], {}, {}, [], {}, 0
        # Migrate legacy 'event_name' key to 'event' on all history records
        history = data.get("history", [])
        for m in history:
            if m.get('event_name') and not m.get('event'):
                m['event'] = m.pop('event_name')
            elif 'event_name' in m:
                del m['event_name']

        return (data["teams"],
                data.get("aliases", {}),
                history,
                data.get("peaks", {}),
                data.get("event_tiers", {}),
                data.get("adjustments", []),
                data.get("provisional_teams", {}),
                data.get("total_imports", 0))
    except Exception as e:
        error_log(f"Failed to parse save code: {e}")
        raise

def _backup_path(tier: int) -> str:
    _backup_dir = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "backup")
    return os.path.join(_backup_dir, f"backup_{tier}.save")

def _rotate_backups():
    """
    Write to the single highest backup tier whose interval divides total_imports.

      backup_1.save — every   5 imports
      backup_2.save — every  10 imports  (supersedes backup_1 when both qualify)
      backup_3.save — every  20 imports  (supersedes backup_1 and backup_2)
      backup_4.save — every  50 imports
      backup_5.save — every 100 imports  (supersedes all lower tiers)

    Called AFTER data.save has been written so the backup reflects the
    just-committed state. Does nothing if total_imports is 0 or no tier
    interval divides it.
    """
    if total_imports <= 0:
        return
    _backup_dir = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "backup")
    os.makedirs(_backup_dir, exist_ok=True)
    try:
        # Find the highest tier (largest interval) that divides total_imports
        qualifying = [
            (tier, interval)
            for tier, interval in BACKUP_TIERS.items()
            if interval > 0 and total_imports % interval == 0
        ]
        if not qualifying:
            return
        best_tier, _ = max(qualifying, key=lambda x: x[1])
        import shutil
        shutil.copy2(SAVE_FILE, _backup_path(best_tier))
    except Exception as e:
        error_log(f"Backup rotation failed: {e}")

def save_all(silent: bool = False) -> bool:
    """
    Write all state to data.save (atomic), then update the tiered backup
    if total_imports has reached a tier threshold.
    """
    global unsaved_changes
    os.makedirs(os.path.dirname(SAVE_FILE), exist_ok=True)
    temp_file = SAVE_FILE + ".tmp"
    
    try:
        # 1. Build save code
        save_code = _build_save_code(teams, aliases, history)
        if not save_code:
            if not silent:
                logger.error("Failed to generate save data.")
            return False
        
        # 2. Write to TEMP file first (atomic save)
        with open(temp_file, "w", encoding='utf-8') as f:
            f.write(save_code)
            f.flush()
            os.fsync(f.fileno())
        
        # 3. Verify temp file
        if os.path.exists(temp_file):
            file_size = os.path.getsize(temp_file)
            if file_size > 100:
                # 4. Atomic rename (replaces old file safely)
                os.replace(temp_file, SAVE_FILE)
                if not silent:
                    logger.info(f"Save successful! ({file_size} bytes)")
                unsaved_changes = False
                # 5. Update tiered backup now that new state is committed
                _rotate_backups()
                return True
            else:
                logger.error(f"Save file too small: {file_size} bytes")
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                return False
        else:
            logger.error("Temp save file not created after write")
            return False
            
    except PermissionError:
        logger.error("Permission denied writing save file")
        if not silent:
            print(">>> ERROR: Permission denied! Check if file is open in another program.")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return False
    except Exception as e:
        logger.error(f"Save failed: {type(e).__name__}: {e}")
        if not silent:
            print(f">>> ERROR: Save failed! {type(e).__name__}: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return False

def load_all():
    """Load state from data.save. Auto-recovers from backup if corrupted."""
    global teams, aliases, history, peak_ratings, event_tiers, adjustments, unsaved_changes, total_imports
    
    _backup_dir = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "backup")
    files_to_try = [SAVE_FILE] + [
        os.path.join(_backup_dir, f"backup_{i}.save") for i in range(1, 4)
    ]
    
    for filename in files_to_try:
        if not os.path.exists(filename):
            continue
            
        try:
            with open(filename, "r") as f:
                content = f.read().strip()
                if not content:
                    continue
                    
                t, a, h, pk, et, adj, prov, ti = _parse_save_code(content)
                
                teams.clear(); teams.update(t)
                aliases.clear(); aliases.update(a)
                history.clear(); history.extend(h)
                peak_ratings.clear(); peak_ratings.update(pk)
                event_tiers.clear(); event_tiers.update(et)
                adjustments[:] = adj
                provisional_teams.clear(); provisional_teams.update({k: int(v) for k, v in prov.items()})
                total_imports = ti
                
                if filename == SAVE_FILE:
                    print(f">>> Loaded {len(history)} matches from {filename}")
                else:
                    print(f">>> WARNING: Loaded from {filename} (Primary save corrupted/missing)")
                unsaved_changes = False
                return
        except Exception as e:
            error_log(f"Failed to load {filename}: {e}")
            continue
    
    print(">>> No save file found. Starting fresh.")
    unsaved_changes = False

def load_logic(raw_code):
    """
    Load state directly from a save code string (used for import).
    
    Used when importing save codes from other users or backup files.
    """
    global teams, aliases, history, peak_ratings, event_tiers, adjustments, provisional_teams, total_imports
    try:
        t, a, h, pk, et, adj, prov, ti = _parse_save_code(raw_code)
        teams.clear(); teams.update(t)
        aliases.clear(); aliases.update(a)
        history.clear(); history.extend(h)
        peak_ratings.clear(); peak_ratings.update(pk)
        event_tiers.clear(); event_tiers.update(et)
        adjustments[:] = adj
        provisional_teams.clear(); provisional_teams.update({k: int(v) for k, v in prov.items()})
        total_imports = ti
    except Exception as e:
        error_log(f"Load logic failed: {e}")
        raise

# Legacy wrappers for compatibility
def load_aliases(): pass
def save_aliases(): save_all()
def load_history(): return history
def save_history(h):
    global history
    history = h
    return save_all()

# =============================================================================
# === HELPER FUNCTIONS (INPUT, CLIPBOARD, UTILS) ===
# =============================================================================

def pick_date_range():
    """
    Prompt user for date range with 4 quick options + custom.
    Returns (start_date, end_date) or (None, None).
    Maximum 10 options (1-6 functions + 0 back).
    """
    from datetime import datetime, timedelta
    
    # Get all match dates from history
    match_dates = []
    for m in history:
        match_date_str = m.get('date', 'N/A')
        if match_date_str and match_date_str != 'N/A':
            try:
                match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d").date()
                match_dates.append(match_date)
            except:
                pass
    
    if not match_dates:
        print("  [!] No match history found.")
        return None, None
    
    earliest_match = min(match_dates)
    latest_match = min(datetime.now().date(), max(match_dates))
    today = datetime.now().date()
    
    
    while True:
        print_menu(
            "SELECT DATE RANGE",
            [
                ("1", "Maximum Range  (all matches)"),
                ("2", "Last X Months"),
                ("3", "Last X Weeks"),
                ("4", "Last X Days"),
                ("5", "Custom Date Range"),
                (None, None),
                ("0", "Back"),
            ],
            subtitle=f"Data: {earliest_match} → {latest_match}  ({len(history)} matches)",
        )

        choice = input("Select: ").strip()
        
        if choice == '0':
            return None, None
        
        if choice == '1':
            start_date = earliest_match
            end_date = latest_match
            
            today = datetime.now().date()
            if latest_match < today:
                days_extension = (today - latest_match).days
                extend_choice = get_cmd(check_cmd(
                    input(f"\n  Extend to today's date ({today})? Adds {days_extension} days for depreciation curves. (y/n): ")
                ))
                if extend_choice == 'y':
                    end_date = today
                    print(f"\n  [OK] Extended range: {start_date} to {end_date} (includes depreciation to today)")
                else:
                    print(f"\n  [OK] Using maximum range: {start_date} to {end_date} (latest match)")
            else:
                print(f"\n  [OK] Using maximum range: {start_date} to {end_date}")
            
            range_days = (end_date - start_date).days
            print(f"  [OK] Range: {range_days} days, {len(history)} matches\n")
            return start_date, end_date
        
        elif choice == '2':
            months_raw = input("  How many months? (1-24): ").strip()
            if get_cmd(months_raw) == '0':
                continue
            try:
                months = int(months_raw)
                if months < 1 or months > 24:
                    print("  [!] Please enter 1-24 months.")
                    continue
                
                end_date = latest_match
                start_date = end_date - timedelta(days=months * 30)
                
                if start_date < earliest_match:
                    print(f"  [!] Warning: Only data from {earliest_match} is available.")
                    print(f"  [OK] Adjusted start date to: {earliest_match}")
                    start_date = earliest_match
                
                matches_in_range = [m for m in history 
                                   if m.get('date', 'N/A') != 'N/A' 
                                   and start_date <= datetime.strptime(m.get('date', '')[:10], "%Y-%m-%d").date() <= end_date]
                
                if not matches_in_range:
                    print(f"  [!] WARNING: No matches found in this range.")
                    print(f"  Earliest available: {earliest_match}")
                    retry = input("  Pick different range? (y/n): ").strip().lower()
                    if retry == 'y':
                        continue
                
                print(f"\n  [OK] Range: {start_date} to {end_date}")
                print(f"  [OK] Matches in range: {len(matches_in_range)}\n")
                return start_date, end_date
            except ValueError:
                print("  [!] Invalid number. Try again.")
                continue
        
        elif choice == '3':
            weeks_raw = input("  How many weeks? (1-52): ").strip()
            if get_cmd(weeks_raw) == '0':
                continue
            try:
                weeks = int(weeks_raw)
                if weeks < 1 or weeks > 52:
                    print("  [!] Please enter 1-52 weeks.")
                    continue
                
                end_date = latest_match
                start_date = end_date - timedelta(weeks=weeks)
                
                if start_date < earliest_match:
                    print(f"  [!] Warning: Only data from {earliest_match} is available.")
                    print(f"  [OK] Adjusted start date to: {earliest_match}")
                    start_date = earliest_match
                
                matches_in_range = [m for m in history 
                                   if m.get('date', 'N/A') != 'N/A' 
                                   and start_date <= datetime.strptime(m.get('date', '')[:10], "%Y-%m-%d").date() <= end_date]
                
                if not matches_in_range:
                    print(f"  [!] WARNING: No matches found in this range.")
                    print(f"  Earliest available: {earliest_match}")
                    retry = input("  Pick different range? (y/n): ").strip().lower()
                    if retry == 'y':
                        continue
                
                print(f"\n  [OK] Range: {start_date} to {end_date}")
                print(f"  [OK] Matches in range: {len(matches_in_range)}\n")
                return start_date, end_date
            except ValueError:
                print("  [!] Invalid number. Try again.")
                continue
        
        elif choice == '4':
            days_raw = input("  How many days? (1-365): ").strip()
            if get_cmd(days_raw) == '0':
                continue
            try:
                days = int(days_raw)
                if days < 1 or days > 365:
                    print("  [!] Please enter 1-365 days.")
                    continue
                
                end_date = latest_match
                start_date = end_date - timedelta(days=days)
                
                if start_date < earliest_match:
                    print(f"  [!] Warning: Only data from {earliest_match} is available.")
                    print(f"  [OK] Adjusted start date to: {earliest_match}")
                    start_date = earliest_match
                
                matches_in_range = [m for m in history 
                                   if m.get('date', 'N/A') != 'N/A' 
                                   and start_date <= datetime.strptime(m.get('date', '')[:10], "%Y-%m-%d").date() <= end_date]
                
                if not matches_in_range:
                    print(f"  [!] WARNING: No matches found in this range.")
                    print(f"  Earliest available: {earliest_match}")
                    retry = input("  Pick different range? (y/n): ").strip().lower()
                    if retry == 'y':
                        continue
                
                print(f"\n  [OK] Range: {start_date} to {end_date}")
                print(f"  [OK] Matches in range: {len(matches_in_range)}\n")
                return start_date, end_date
            except ValueError:
                print("  [!] Invalid number. Try again.")
                continue
        
        elif choice == '5':
            print("\n  Enter start date (YYYY-MM-DD) or '0' to go back:")
            start_raw = input("  > ").strip()
            if get_cmd(start_raw) == '0':
                continue
            try:
                start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
                if start_date < earliest_match:
                    print(f"  [!] No matches before {earliest_match}")
                    continue
                if start_date > latest_match:
                    print(f"  [!] Start date cannot be after latest match ({latest_match})")
                    continue
            except ValueError:
                print("  [!] Invalid format. Use YYYY-MM-DD.")
                continue
            
            print(f"\n  Enter end date (YYYY-MM-DD) or press Enter for latest match ({latest_match}):")
            end_raw = input("  > ").strip()
            if get_cmd(end_raw) == '0':
                continue
            if not end_raw:
                end_date = latest_match
            else:
                try:
                    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
                    if end_date > latest_match:
                        print(f"  [!] End date cannot be after latest match ({latest_match})")
                        continue
                    if end_date < start_date:
                        print(f"  [!] End date must be after start date ({start_date})")
                        continue
                except ValueError:
                    print("  [!] Invalid format. Use YYYY-MM-DD.")
                    continue
            
            matches_in_range = [m for m in history 
                               if m.get('date', 'N/A') != 'N/A' 
                               and start_date <= datetime.strptime(m.get('date', '')[:10], "%Y-%m-%d").date() <= end_date]
            
            if not matches_in_range:
                print(f"  [!] WARNING: No matches found in this range.")
                retry = input("  Pick different range? (y/n): ").strip().lower()
                if retry == 'y':
                    continue
            
            range_days = (end_date - start_date).days
            print(f"\n  [OK] Selected range: {start_date} to {end_date} ({range_days} days)")
            print(f"  [OK] Matches in range: {len(matches_in_range)}\n")
            return start_date, end_date
        
        else:
            print("  [!] Invalid option. Select 1-5 or 0.")


def update_peak(name, pts, date_str):
    """
    Update peak rating for a team if current points exceed recorded peak.
    """
    ranked = get_sorted_rankings(include_archived=True)
    rank = ranked.index(name) + 1 if name in ranked else 0
    if name not in peak_ratings or pts > peak_ratings[name]["points"]:
        peak_ratings[name] = {"points": pts, "date": date_str, "rank": rank}

def check_cmd(user_input):
    """
    Secret commands: 'quit' to exit, 'back' to return, 'restart' to reboot.
    """
    cmd = str(user_input).lower().strip()
    if cmd == "quit":
        sys.exit()
    if cmd == "menu":
        raise MenuException()
    if cmd == "restart":
        print("\nRestarting program...")
        if os.name == 'nt':  
            subprocess.Popen(f'ping 127.0.0.1 -n 2 > nul && start "" {sys.executable} {" ".join(sys.argv)}', shell=True)
        else:  
            subprocess.Popen(f'sleep 1 && {sys.executable} {" ".join(sys.argv)}', shell=True)
        sys.exit()
    return user_input

def get_cmd(user_input):
    """Internal helper to get lowercase command for menu logic."""
    return str(user_input).lower().strip()

def find_team(name: str) -> Optional[str]:
    """
    Find team by exact name or alias.
    Returns the full team name if found, None otherwise.
    
    Priority: Exact match > Alias match
    """
    if not name or not name.strip():
        return None
    
    name_lower = name.lower().strip()
    
    # 1. Exact match
    for team_key in teams:
        if team_key.lower() == name_lower:
            return team_key
    
    # 2. Alias match
    resolved = aliases.get(name_lower)
    if resolved:
        for team_key in teams:
            if team_key.lower() == resolved.lower():
                return team_key

    return None

def is_provisional(team_name: str) -> bool:
    """Return True if team is still in provisional period."""
    return team_name in provisional_teams

def get_provisional_k(team_name: str) -> float:
    """Return K multiplier for a provisional team based on matches played so far."""
    matches_played = provisional_teams.get(team_name, 0)
    match_number = matches_played + 1  # about to play this match
    return PROVISIONAL_K_FACTORS.get(match_number, 1.0)

def increment_provisional(team_name: str) -> None:
    """Increment provisional match count, graduating team when threshold is reached."""
    if team_name not in provisional_teams:
        return
    provisional_teams[team_name] += 1
    if provisional_teams[team_name] >= PROVISIONAL_MATCH_THRESHOLD:
        del provisional_teams[team_name]
        print(f"  >>> '{team_name}' has graduated from provisional status and entered the rankings.")


def is_team_archived(team_name: str, today=None, index: Optional[Dict[str, List[datetime]]] = None) -> bool:
    """
    Return True if a team hasn't played a match in INACTIVE_ARCHIVE_DAYS+ days.

    This sits ON TOP of (not instead of) the existing depreciation curve. Depreciation
    still discounts an inactive team's rating for match-calculation purposes (capped at
    ~25% off, held forever after ~75 days — see calculate_depreciation), but that alone
    never actually clears anyone out: the team stays in `teams` and in every ranking/graph
    forever. Valve's own VRS avoids this by recomputing each team's score from only a
    rolling ~6-month window of results, so a team with nothing left in that window just
    scores ~0 and falls off the list — not deleted, just not shown.

    is_team_archived() reproduces that "falls off the list" behavior on top of CSRS's
    rating math: past INACTIVE_ARCHIVE_DAYS (default 180, matching VRS's window) a team is
    treated as archived and excluded from the default rankings/graphs view by
    get_sorted_rankings()/display_rankings(). It is never deleted, its rating and history
    are untouched, and it automatically becomes active again the moment it plays a new
    match (there is no separate "restore" step).

    A team with no match history at all (freshly created, never played) is NOT archived —
    there's nothing to have gone stale.
    """
    if today is None:
        today = datetime.now().date()
    last_match = get_team_last_match_date_before(team_name, index=index)
    if last_match is None:
        return False
    return (today - last_match.date()).days > INACTIVE_ARCHIVE_DAYS


def get_sorted_rankings(include_archived: bool = False):
    """
    Return list of team names sorted by points (High to Low).

    By default, teams archived for long-term inactivity (see is_team_archived) are
    excluded — mirroring how Valve's VRS drops teams with nothing left in its rolling
    window instead of listing them forever at a discounted rating. Pass
    include_archived=True for callers that need a specific team's rank/position
    regardless of its activity status (e.g. compare_teams, simulate_match), to avoid
    'team not found in rankings' errors for archived teams.
    """
    # Consider depreciation for accurate ranking
    today = datetime.now().date()
    team_ratings = []
    date_index = build_match_date_index(history)

    for name, points in teams.items():
        last_match = get_team_last_match_date_before(name, index=date_index)
        display_rating = points
        
        if last_match:
            days_inactive = (today - last_match.date()).days
            if not include_archived and days_inactive > INACTIVE_ARCHIVE_DAYS:
                continue
            if days_inactive > DEPRECIATION_THRESHOLD:
                display_rating = calculate_depreciation(points, days_inactive, name)
        
        team_ratings.append((name, display_rating))
    
    return [name for name, rating in sorted(team_ratings, key=lambda x: x[1], reverse=True)]

def copy_to_clipboard(text):
    """Attempt to copy text to system clipboard using tkinter."""
    try:
        r = Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return True
    except:
        return False

def select_environment():
    """Prompt user to select match environment (Online/LAN)."""
    env_map = {"1": "ONLINE", "2": "LAN"}
    print("Environment: 1. Online  2. LAN")
    while True:
        raw = check_cmd(input("Select: "))
        if get_cmd(raw) == "back": return None
        if raw.strip() in env_map:
            return env_map[raw.strip()]
        print("  Invalid choice. Enter 1 or 2.")

def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as Xh Ym Zs."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def print_progress(current: int, total: int, prefix: str = '', length: int = 40) -> None:
    """
    Print a text-based progress bar.
    
    Example: Processing: [████████████░░░░░░░░] 60% (60/100)
    """
    percent = 100 * (current / float(total))
    filled = int(length * current // total)
    bar = '█' * filled + '░' * (length - filled)
    print(f'\r{prefix} [{bar}] {percent:.1f}% ({current}/{total})', end='', flush=True)
    if current == total:
        print()  # Newline when complete

def validate_match_entry(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Validate a match entry before saving.
    Returns (is_valid, error_message).
    """
    # Check required fields
    required_fields = ['date', 't1', 't2', 'tier', 'env']
    for field in required_fields:
        if field not in entry:
            return False, f"Missing required field: {field}"
    
    # Check teams
    t1 = entry.get('t1', {})
    t2 = entry.get('t2', {})
    
    if not t1.get('name') or not t2.get('name'):
        return False, "Missing team name(s)"
    
    if t1.get('name') == t2.get('name'):
        return False, "Both teams cannot be the same"
    
    # Check scores
    t1_score = t1.get('score', -1)
    t2_score = t2.get('score', -1)
    
    if not isinstance(t1_score, (int, float)) or not isinstance(t2_score, (int, float)):
        return False, "Scores must be numbers"
    
    if t1_score < 0 or t2_score < 0:
        return False, "Scores cannot be negative"
    
    if t1_score == t2_score:
        return False, "Matches cannot end in a draw (CS:GO/CS2)"
    
    # Check ratings
    if t1.get('pts_before') is None or t2.get('pts_before') is None:
        return False, "Missing rating data (pts_before)"
    
    # Check tier
    tier = entry.get('tier', '')
    if tier not in ['S+', 'S', 'A', 'B', 'C', 'D', 'E']:
        return False, f"Invalid tier: {tier}"
    
    # Check environment
    env = entry.get('env', '')
    if env not in ['ONLINE', 'LAN', 'STUDIO', 'STAGE']:
        return False, f"Invalid environment: {env}"
    
    return True, ""

def print_success(message: str) -> None:
    """Print a success message with consistent formatting."""
    print(f">>> {message}")

def print_warning(message: str) -> None:
    """Print a warning message with consistent formatting."""
    print(f"[!] {message}")

def print_info(message: str) -> None:
    """Print an info message with consistent formatting."""
    print(f"[OK] {message}")

def print_error(message: str) -> None:
    """Print an error message with consistent formatting."""
    print(f"[ERROR] {message}")
    logger.error(message)

def print_menu(title: str, options: list, subtitle: str = None) -> None:
    """Print a box-bordered menu. options = list of (key, label) tuples; key=None inserts a divider."""
    W = 44
    inner = W - 2
    top    = "╔" + "═" * inner + "╗"
    mid    = "╠" + "═" * inner + "╣"
    bottom = "╚" + "═" * inner + "╝"
    side   = "║"
    def pad(text):
        return f"{side} {text:<{inner - 1}}{side}"
    def divider():
        return "╟" + "─" * inner + "╢"
    print()
    print(top)
    print(pad(f"  {title}"))
    if subtitle:
        sub = subtitle if len(subtitle) <= inner - 3 else subtitle[:inner - 6] + "..."
        print(pad(f"  {sub}"))
    print(mid)
    for key, label in options:
        if key is None:
            print(divider())
        else:
            print(pad(f"  [{key}]  {label}"))
    print(bottom)

def _print_match_entry(index, m):
    """
    Helper to format a single match entry for printing.
    """
    fmt_shift = lambda s: f"(+{s})" if s > 0 else (f"({s})" if s < 0 else "(-)")
    fmt_pts = lambda a, b: f"(+{int(round(a-b))})" if a > b else (f"({int(round(a-b))})" if a < b else "(-)")
    gf_tag = f" [{m.get('match_stage')}]" if m.get('match_stage') else (" [GRAND FINAL]" if m.get('grand_final', False) else "")
    ff_tag = ""
    if m.get('forfeit') == 'team1':
        ff_tag = f" [FORFEIT: {m.get('t1', {}).get('name', '?')}]"
    elif m.get('forfeit') == 'team2':
        ff_tag = f" [FORFEIT: {m.get('t2', {}).get('name', '?')}]"
    
    t1 = m.get('t1', {})
    t2 = m.get('t2', {})
    
    t1_name = t1.get('name', 'Unknown')
    t2_name = t2.get('name', 'Unknown')
    t1_score = t1.get('score', 0)
    t2_score = t2.get('score', 0)
    
    if t1_score > t2_score:
        winner = t1_name
    elif t2_score > t1_score:
        winner = t2_name
    else:
        winner = "Draw"
        
    event_name = m.get('event', 'N/A')
    match_date = m.get('date', 'N/A')
    tier = m.get('tier', '?')
    env = m.get('env', '?')
    
    print(f"#{index} | {match_date} | Tier {tier} | {env} | {event_name}{gf_tag}{ff_tag}")
    print(f"  {t1_name} {t1_score} - {t2_score} {t2_name}  >>  Winner: {winner}")
    
    t1_pts_before = t1.get('pts_before')
    t1_pts_after = t1.get('pts_after')
    t1_rank_shift = t1.get('rank_shift')
    
    t2_pts_before = t2.get('pts_before')
    t2_pts_after = t2.get('pts_after')
    t2_rank_shift = t2.get('rank_shift')
    
    if t1_pts_before is not None and t1_pts_after is not None:
        t1_pts_str = f"{int(t1_pts_before)} -> {int(t1_pts_after)} pts {fmt_pts(t1_pts_after, t1_pts_before)}"
    else:
        t1_pts_str = "N/A"
        
    if t2_pts_before is not None and t2_pts_after is not None:
        t2_pts_str = f"{int(t2_pts_before)} -> {int(t2_pts_after)} pts {fmt_pts(t2_pts_after, t2_pts_before)}"
    else:
        t2_pts_str = "N/A"

    if t1_rank_shift is not None:
        t1_rank_str = f"Rank: {fmt_shift(t1_rank_shift)}"
    else:
        t1_rank_str = "Rank: N/A"

    if t2_rank_shift is not None:
        t2_rank_str = f"Rank: {fmt_shift(t2_rank_shift)}"
    else:
        t2_rank_str = "Rank: N/A"

    print(f"  {t1_name}: {t1_pts_str}  {t1_rank_str}")
    print(f"  {t2_name}: {t2_pts_str}  {t2_rank_str}")
    print()

# =============================================================================
# === MENU: FUTURE UPDATES ===
# =============================================================================

def future_updates_menu():
    """
    Display planned features organized by priority.
    """
    prioritized = [
        ("\n=== [OK] COMPLETED FEATURES ===", ""),
        ("1. Improved Match Import Reliability", "Better scraper with lazy loading, fallback selectors, GF detection"),
        ("2. Head-to-Head Win Probability Calculator", "Bo3/Bo5 predictions with map score breakdown"),
        ("3. Elite Teams Over Time Graph", "Visual graph showing rating changes for top teams"),
        ("4. Date Range Selection (4 Options)", "Max/Months/Weeks/Days + custom, end=latest match"),
        ("5. Keyboard Shortcuts (menu/quit/restart)", "Type 'menu', 'quit', 'restart' in most prompts"),
        ("6. Form Table with Streak Tracker", "Shows last 7 match results (W/L streak)"),
        ("7. Duplicate URL Warning on Import", "Alerts if match URL already exists"),
        ("8. Backup System (5 Rotating Backups)", "Auto-backup before saves, restore from backup menu"),
        ("9. VRS Points Caching", "Prevents repeated requests for same date"),
        ("10. Save Verification After Import", "Confirms data written to disk before reporting success"),
        ("11. Elite Graph Depreciation Curves", "Visual depreciation after 7 days inactivity"),
        ("12. Elite Graph Transition Markers", "Circle markers at flat-to-diagonal transitions"),
        ("13. Elite Graph Dimmed Lines Below 850", "Teams below elite threshold visually de-emphasized"),
        ("14. Analyze Team Form (Decluttered)", "Clean single-screen form breakdown with bars"),
        ("15. Improved Form Insights System", "Tiered insights: win rate, maps, competition, streaks, trends"),
        ("16. Fixed Streak Detection", "Counts consecutive results, not total streak string"),
        ("17. Filter History by Tier/Environment", "View only S-Tier or Stage matches"),
        ("18. Map Score Distribution Histogram", "Vertical bar chart for 2-0, 2-1, 3-0, etc."),
        
        ("\n=== [~] HIGH PRIORITY ===", ""),
        ("19. Search Teams by Partial Name", "Type 'Vit' instead of 'Vitality' in all menus"),
        ("20. Bulk Edit Matches by Event", "Change tier/environment for all matches from one event at once"),
        ("21. Show Save File Info in Menu", "Display data.save size and last modified date"),
        ("22. Fix Import Window Termination/Hang Issues", "Bug: GUI hangs when closing after import"),
        ("23. Undo Last Delete", "Recover accidentally deleted matches from backup (one-click)"),
        
        ("\n=== [~] MEDIUM PRIORITY ===", ""),
        ("24. Progress Bar for Resimulation", "Visual feedback when resimulating 100+ matches"),
        ("25. Team Quick Stats on Selection", "Show current points, form, trend when selecting a team"),
        ("26. Monthly/Yearly Summary Report", "See which teams gained/lost most points in a period"),
        ("27. Peak Rating Profile Lookup", "Standalone lookup for team peak rating history"),
        ("28. Estimated Time for Resimulation", "Show ~15 seconds before starting long resims"),
        ("29. Skip Unchanged Matches on Resim", "Only recalculate matches after edited/deleted ones"),
        
        ("\n=== [~] ANALYTICS ENHANCEMENTS ===", ""),
        ("30. Team Performance vs Tier", "Show how teams perform at S-Tier vs C-Tier events"),
        ("31. Cache Form Calculations", "Store form scores, recalculate only when history changes"),
        ("32. Lazy Load History for Display", "Show 50 matches at a time with Load More for large histories"),
        ("33. Export Form Analysis to File", "Save team form breakdown as text/CSV"),
        
        ("\n=== [~] LOW PRIORITY / POLISH ===", ""),
        ("34. Color-Coded Output", "Green for gains, red for losses (if terminal supports it)"),
        ("35. Team Logo/Flag Display", "Show emojis or ASCII art for teams (cosmetic)"),
        ("36. Orphaned Team Detection", "Find teams in history that no longer exist in roster"),
        ("37. Reorder Menus", "Feature: Customizable menu layout"),
        ("38. Export Graph to PNG/CSV", "Save elite teams graph or data for sharing"),
        ("39. Compare Specific Teams Graph", "Select 2-5 teams to compare instead of all elite"),
        ("40. Form Analysis Trend Graph", "Mini ASCII graph of form over last 15 matches"),
    ]
    
    completed_count = len([i for i in prioritized if i[0].startswith('\n=== [OK]')]) - 1
    pending_count = len([i for i in prioritized if i[0].startswith('\n=== [~]')])
    
    print("\n" + "="*70)
    print(" FUTURE UPDATES & FEATURE REQUESTS")
    print("="*70)
    print(f"  Completed: {completed_count} features [OK]")
    print(f"  Pending: {pending_count} features [~]")
    print("="*70)
    for item, reason in prioritized:
        if item.startswith("\n==="):
            print(f"\n{item}")
            print("-"*70)
        else:
            print(f"  {item}")
            if reason:
                print(f"    -> {reason}")
    print("\n" + "="*70)
    print(" Note: Features marked HIGH PRIORITY will be implemented first.")
    print("       Submit bug reports to csrs_error.log if issues occur.")
    print("="*70)


# =============================================================================
# === MENU: SAVE/LOAD ===
# =============================================================================

def quicksave() -> None:
    """Quick save to data.save with backup."""
    if save_all():
        print_info("Quicksave updated. Backup created.")
    else:
        print_error("Save failed!")


def export_save_code() -> None:
    """Export save data as a base64 code string."""
    save_code = _build_save_code(teams, aliases, history)
    if not save_code:
        print(">>> ERROR: Could not generate save code.")
        return
    print(f"\n{'='*40}\nYOUR SAVE CODE:\n{save_code}\n{'='*40}")
    copy_choice = get_cmd(check_cmd(input("Copy to clipboard? (y/n) or '0' to skip: ")))
    if copy_choice == 'y':
        print(">>> Copied!" if copy_to_clipboard(save_code) else ">>> Clipboard error.")


def export_save_file() -> None:
    """Export save data to a .save file."""
    filename = check_cmd(input("Enter filename: ")).strip()
    if get_cmd(filename) in ['back', '0']:
        return
    if not filename.endswith(".save"):
        filename += ".save"
    save_code = _build_save_code(teams, aliases, history)
    if not save_code:
        print(">>> ERROR: Could not generate save data.")
        return
    with open(filename, "w", encoding='utf-8') as f:
        f.write(save_code)
    print(f">>> Exported to {filename}")


def import_save_code() -> None:
    """Import save data from a base64 code string."""
    raw_code = check_cmd(input("\nPaste your Save Code: ")).strip()
    if get_cmd(raw_code) in ['back', '0']:
        return
    
    if not raw_code or len(raw_code) < 50:
        print_warning("Save code looks too short. Please check and try again.")
        return
    
    print_info("Creating backup before import...")
    _rotate_backups()
    
    try:
        load_logic(raw_code)
        save_all()
        print_success("Code Imported!")
        print_info("Original data backed up to backup_1.save")
    except Exception as e:
        logger.error(f"Import code failed: {e}")
        print_error("Invalid Code or corrupted data.")
        print_info("Your original data is safe (check backup_1.save)")


def import_save_file() -> None:
    """Import save data from a .save file."""
    filename = check_cmd(input("Enter filename to load: ")).strip()
    if get_cmd(filename) in ['back', '0']:
        return
    if not filename.endswith(".save"):
        filename += ".save"
    if os.path.exists(filename):
        with open(filename, "r", encoding='utf-8') as f:
            raw_data = f.read().strip()
        try:
            load_logic(raw_data)
            save_all()
            print(f">>> SUCCESS: Loaded {filename}")
        except Exception as e:
            logger.error(f"Import file failed: {e}")
            print(">>> ERROR: File invalid.")
    else:
        print(">>> ERROR: File not found.")


def restore_from_backup() -> None:
    """Restore data from a backup file."""
    print("\n--- Restore from Backup ---")
    _backup_dir = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "backup")
    backups = [
        os.path.join(_backup_dir, f"backup_{i}.save")
        for i in range(1, 6)
        if os.path.exists(os.path.join(_backup_dir, f"backup_{i}.save"))
    ]
    if not backups:
        print(">>> No backup files found.")
        return
    for i, b in enumerate(backups, 1):
        print(f"  {i}. {os.path.basename(b)}")
    print("  0. Back")
    try:
        sel_raw = check_cmd(input("Select backup to restore: "))
        if get_cmd(sel_raw) in ['back', '0']:
            return
        sel = int(sel_raw) - 1
        if 0 <= sel < len(backups):
            confirm = get_cmd(check_cmd(input(f"Restore from {os.path.basename(backups[sel])}? This will overwrite current data. (y/n): ")))
            if confirm == 'y':
                with open(backups[sel], "r", encoding='utf-8') as f:
                    load_logic(f.read().strip())
                save_all()
                print(">>> SUCCESS: Backup restored.")
        else:
            print(">>> Invalid selection.")
    except ValueError:
        print(">>> Invalid input.")


def list_save_files() -> None:
    """List all .save files in the structured save directories."""
    _data_dir = os.environ.get("CSRS_DATA_DIR", ".")
    _main_dir   = os.path.join(_data_dir, "save", "main")
    _backup_dir = os.path.join(_data_dir, "save", "backup")
    print("\nAvailable Save Files:")
    found = False
    for d in [_main_dir, _backup_dir]:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith('.save'):
                    full = os.path.join(d, f)
                    rel  = os.path.relpath(full, _data_dir)
                    print(f"  - {rel} ({os.path.getsize(full)} bytes)")
                    found = True
    if not found:
        print("  - No .save files found.")


def save_load_menu() -> None:
    """Menu for exporting, importing, backing up, and restoring save data."""
    options = [
        ('1', 'Quicksave', quicksave),
        ('2', 'Export Save Code', export_save_code),
        ('3', 'Export Save File', export_save_file),
        ('4', 'Import Save Code', import_save_code),
        ('5', 'Import Save File', import_save_file),
        ('6', 'Restore from Backup', restore_from_backup),
        ('7', 'List Save Files', list_save_files),
        ('0', 'Back', None),
    ]
    
    while True:
        save_code = _build_save_code(teams, aliases, history)
        print_menu(
            "SAVE / LOAD",
            [
                ("1", "Quicksave"),
                (None, None),
                ("2", "Export Save Code"),
                ("3", "Export Save File"),
                ("4", "Import Save Code"),
                ("5", "Import Save File"),
                (None, None),
                ("6", "Restore from Backup"),
                ("7", "List Save Files"),
                (None, None),
                ("h", "Help  (explain options)"),
                ("0", "Back"),
            ],
            subtitle=f"Matches: {len(history)}  ·  Teams: {len(teams)}",
        )
        
        raw_choice = check_cmd(input("Select: ")).strip()
        choice = get_cmd(raw_choice)
        
        if choice in ['0', 'back']:
            break
        
        if choice == 'h':
            print("\n--- Save/Load Help ---")
            print("  1. Quicksave: Save current state to data.save (auto-backup created)")
            print("  2. Export Code: Generate a text code to share your data")
            print("  3. Export File: Save to a custom .save file")
            print("  4. Import Code: Load data from a shared text code")
            print("  5. Import File: Load from a .save file")
            print("  6. Restore Backup: Recover from automatic backup (up to 5)")
            print("  7. List Files: Show all .save files in directory")
            print("\n  Tip: 5 rotating backups are kept automatically!")
            input("\nPress Enter to continue...")
            continue
        
        found = False
        for num, _, func in options:
            if choice == num:
                func()
                found = True
                break
        if not found:
            print_warning("Invalid choice. Try again.")


def reload_quicksave() -> None:
    """Reload data from the main save file."""
    if os.path.exists(SAVE_FILE):
        confirm = get_cmd(check_cmd(input("Reload from data.save? This will discard unsaved changes. (y/n) or '0' to cancel: ")))
        if confirm in ['y', 'yes']:
            try:
                with open(SAVE_FILE, "r", encoding='utf-8') as f:
                    load_logic(f.read().strip())
                print(">>> SUCCESS: Data reloaded.")
            except Exception as e:
                logger.error(f"Reload failed: {e}")
                print(">>> ERROR: File corrupted.")
        else:
            print(">>> Cancelled.")
    else:
        print(">>> ERROR: No save file found.")


# =============================================================================
# === DEPRECIATION CALCULATION (APPLIED TO MATCHES) ===
# =============================================================================

def calculate_depreciation(current_rating: float, days_inactive: int, team_name: str = None, form_score: Optional[float] = None) -> float:
    """
    Calculate depreciated rating based on days of inactivity.
    THIS IS APPLIED TO MATCH CALCULATIONS, NOT JUST VISUALS.

    Parameters:
    - current_rating: Team's current rating
    - days_inactive: Number of days since last match
    - team_name: Optional, used to look up form when form_score is not provided
    - form_score: Optional pre-computed form score (0-100). When supplied the internal
                  calculate_form() call is skipped — pass this during resimulation to
                  avoid form being calculated against future history.

    Returns: Depreciated rating (or current rating if no depreciation applies)
    """
    if days_inactive <= DEPRECIATION_THRESHOLD:
        return current_rating

    # Base decay calculation (quadratic curve, capped at 75 days, max 25% loss)
    base_decay = _base_decay_for(days_inactive)

    # Form modifier (better form = slower depreciation)
    form_modifier = 1.0
    resolved_form_score = form_score
    if resolved_form_score is None and team_name:
        form = calculate_form(team_name, n=15, history=history)
        if form:
            resolved_form_score = form[1]
    if resolved_form_score is not None:
        form_modifier = 1.0 - ((resolved_form_score - 50) / 250)
        form_modifier = max(FORM_MODIFIER_MIN, min(FORM_MODIFIER_MAX, form_modifier))

    decay_factor = base_decay * form_modifier
    depreciated_rating = current_rating * (1 - decay_factor)

    return max(RATING_FLOOR, depreciated_rating)


def build_match_date_index(history_list: List[Dict[str, Any]]) -> Dict[str, List[datetime]]:
    """
    Build a {team_name: sorted_list_of_match_datetimes} index from history.

    Used to avoid O(teams x history) scans in get_sorted_rankings and the
    import flow's dep_base loop — build this once per batch, then pass it
    into get_team_last_match_date_before via the `index` parameter.
    """
    index: Dict[str, List[datetime]] = {}
    for m in history_list:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        date_str = m.get('date', 'N/A')
        if not date_str or date_str == 'N/A':
            continue
        try:
            clean = date_str.replace(' UTC', '').strip()
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


def get_team_last_match_date_before(team_name: str, before_date: datetime = None,
                                     index: Optional[Dict[str, List[datetime]]] = None) -> Optional[datetime]:
    """
    Get the datetime of a team's most recent match BEFORE a specific datetime.
    Parses full datetime including time for same-day match ordering.

    If `index` (from build_match_date_index) is provided, uses a binary
    search instead of scanning all of history — pass an index when calling
    this in a loop over many teams.
    """
    if index is not None:
        dates = index.get(team_name)
        if not dates:
            return None
        if before_date is None:
            return dates[-1]
        # bisect_left finds the insertion point for before_date,
        # so dates[idx-1] is the last date strictly less than before_date
        idx = bisect.bisect_left(dates, before_date)
        return dates[idx - 1] if idx > 0 else None

    team_matches = []
    for m in history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team_name or t2_name == team_name:
            date_str = m.get('date', 'N/A')
            if date_str and date_str != 'N/A':
                try:
                    # Try full datetime first (e.g. "2026-06-05 14:30 UTC")
                    clean = date_str.replace(' UTC', '').strip()
                    try:
                        match_date = datetime.strptime(clean, "%Y-%m-%d %H:%M")
                    except ValueError:
                        match_date = datetime.strptime(clean[:10], "%Y-%m-%d")
                    if before_date is None or match_date < before_date:
                        team_matches.append(match_date)
                except:
                    pass

    return max(team_matches) if team_matches else None


def apply_depreciation_to_rating(team_name: str, current_rating: float, match_date: datetime) -> float:
    """
    Apply depreciation to a team's rating before a match based on inactivity.
    THIS IS THE KEY FUNCTION - CALLED BEFORE EVERY MATCH CALCULATION.
    
    Parameters:
    - team_name: Team name
    - current_rating: Stored rating from teams dict
    - match_date: Date of the upcoming match
    
    Returns: Rating after depreciation (if applicable)
    """
    last_match = get_team_last_match_date_before(team_name, match_date)
    
    if last_match is None:
        # No previous match - use stored rating, no depreciation
        return current_rating
    
    days_inactive = (match_date - last_match).days
    depreciated = calculate_depreciation(current_rating, days_inactive, team_name)
    
    return depreciated


# =============================================================================
# === CORE RATING LOGIC (ELO CALCULATION) ===
# =============================================================================

def calculate_points(team_points, opponent_points, result, map_diff, tier='A', env='LAN', is_grand_final=False, team_form_adj=0, opponent_form_adj=0, opp_is_provisional=False):
    """
    Calculate new rating points based on match result.
    NOTE: Depreciation should be applied BEFORE calling this function.
    """
    # === DIMINISHING RETURNS: Calculate K-factor based on rating ===
    k_win = K_WIN
    k_loss = K_LOSS
    
    if DIMINISHING_RETURNS_ENABLED and team_points >= DIMINISHING_THRESHOLD:
        position = (team_points - DIMINISHING_THRESHOLD) / (DIMINISHING_MAX - DIMINISHING_THRESHOLD)
        position = max(0, min(1, position))  # Clamp between 0 and 1
        
        k_factor_win = 1.0 - (position * (1.0 - DIMINISHING_K_WIN_MIN_PERCENT))
        k_factor_loss = 1.0 - (position * (1.0 - DIMINISHING_K_LOSS_MIN_PERCENT))
        
        k_win = K_WIN * k_factor_win
        k_loss = K_LOSS * k_factor_loss
    
    # === Use effective K based on result ===
    K = k_win if result == 1 else k_loss
    
    tiers = {'S+': 1.5, 'S': 1.4, 'A': 1.2, 'B': 1.0, 'C': 0.85, 'D': 0.7, 'E': 0.55}
    maps = {1: 0.8, 2: 1.0, 3: 1.4}
    # LAN is the new unified environment (replaces STUDIO/STAGE split).
    # STUDIO/STAGE kept as aliases for backward compatibility with existing history records.
    environments = {'ONLINE': 0.8, 'LAN': 1.1, 'STUDIO': 1.1, 'STAGE': 1.1}
    gf_mult = 1.5 if is_grand_final else 1.0
    m_tier, m_map, m_env = tiers.get(tier.upper(), 1.0), maps.get(map_diff, 1.0), environments.get(env.upper(), 1.0)

    # Apply form adjustments to effective ratings
    team_eff = team_points + team_form_adj
    opp_eff = opponent_points + opponent_form_adj

    # Option B: Cap rating differential when opponent is provisional
    if opp_is_provisional:
        diff = opp_eff - team_eff
        if abs(diff) > PROVISIONAL_OPP_DIFF_CAP:
            opp_eff = team_eff + (PROVISIONAL_OPP_DIFF_CAP * (1 if diff > 0 else -1))

    expected = 1 / (1 + 10 ** ((opp_eff - team_eff) / 400))

    # Upset factor based on effective ratings
    if (team_eff > opp_eff and result == 0):      # favored team lost (unexpected)
        upset = team_eff / opp_eff
    elif (team_eff < opp_eff and result == 1):    # underdog won (unexpected)
        upset = opp_eff / team_eff
    else:
        upset = 1.0

    # === Calculate Base Change (Elo Formula) ===
    # MOVED UP: This must be calculated BEFORE Pity Logic which references 'change'
    change = K * (result - expected) * m_tier * m_map * gf_mult * upset

    # === PITY POINTS: For heavy underdog losses (Option B - Underdog Reward) ===
    pity_bonus = 0.0
    if PITY_POINTS_ENABLED and result == 0:
        # This team's rating gap vs opponent, expressed as a percent of a reference rating.
        # The reference floors at MISMATCH_REFERENCE_RATING (protects low-rated brackets,
        # same as before) but grows to match whichever team is actually higher-rated once
        # that clears the floor — so elite matchups, which naturally spread further apart in
        # raw Elo, need a proportionally bigger gap before pity/mismatch fully kicks in.
        pity_reference = max(team_points, opponent_points, MISMATCH_REFERENCE_RATING)
        team_rating_percent = 1.0 - ((opponent_points - team_points) / pity_reference)

        # Sliding threshold: at low elo, require a bigger relative gap before pity triggers
        # at all (mirrors the mismatch penalty's sliding threshold below).
        higher_elo = max(team_points, opponent_points)
        effective_pity_threshold = _sliding_threshold(higher_elo, PITY_THRESHOLD_PERCENT, PITY_THRESHOLD_PERCENT_LOW_ELO)
        effective_pity_max = effective_pity_threshold - _PITY_THRESHOLD_TO_MAX_GAP

        if team_rating_percent <= effective_pity_threshold:  # This team is far enough below opponent
            # Scale pity points based on gap severity
            gap_factor = min(1.0, (effective_pity_threshold - team_rating_percent) / (effective_pity_threshold - effective_pity_max))
            pity_base = {1: 9, 2: 6, 3: 3}.get(map_diff, 3)
            pity_bonus = pity_base * gap_factor * m_tier * m_env * gf_mult
            
            # Ensure minimum pity once threshold is crossed
            if pity_bonus < PITY_MIN_POINTS:
                pity_bonus = PITY_MIN_POINTS
            
            # === NATURAL NET GAIN CURVE: 0 at threshold -> Max at cap ===
            # Calculate position in pity range (0.0 to 1.0)
            gap_from_threshold = effective_pity_threshold - team_rating_percent
            total_range = effective_pity_threshold - effective_pity_max
            scale_position = min(1.0, gap_from_threshold / total_range)
            
            # Define net gain bounds
            MIN_NET_GAIN = 0       # At 75%: break even (not losing)
            MAX_NET_GAIN = 7      # At 65% and below: capped maximum
            
            # Calculate target net gain (smooth interpolation)
            target_net_gain = MIN_NET_GAIN + (scale_position * (MAX_NET_GAIN - MIN_NET_GAIN))
            
            # Adjust pity bonus to achieve target net
            current_net = change + pity_bonus
            if current_net < target_net_gain:
                pity_bonus = target_net_gain - change

    # === MISMATCH PENALTY: Smooth decay for beating much weaker teams (percentage-based) ===
    mismatch_multiplier = 1.0
    if MISMATCH_PENALTY_ENABLED and result == 1:
        # Opponent's rating relative to this team, as a percent of a reference rating that
        # floors at MISMATCH_REFERENCE_RATING but scales up with whichever team is actually
        # higher-rated once that clears the floor — see the matching comment in the pity
        # block above.
        mismatch_reference = max(team_points, opponent_points, MISMATCH_REFERENCE_RATING)
        opponent_percent = 1.0 - ((team_points - opponent_points) / mismatch_reference)

        # Sliding threshold: at low elo, require a bigger relative gap before the mismatch
        # penalty fully zeroes out (or flips negative), mirroring the pity block above.
        higher_elo = max(team_points, opponent_points)
        effective_zero_point = _sliding_threshold(higher_elo, MISMATCH_ZERO_POINT_PERCENT, MISMATCH_ZERO_POINT_PERCENT_LOW_ELO)
        effective_max_penalty_percent = effective_zero_point - _MISMATCH_ZERO_TO_MAXPENALTY_GAP

        if opponent_percent <= MISMATCH_DECAY_PERCENT:
            if opponent_percent > effective_zero_point:
                # Decay from 1.0 to 0.0
                position = (opponent_percent - MISMATCH_DECAY_PERCENT) / (effective_zero_point - MISMATCH_DECAY_PERCENT)
                mismatch_multiplier = 1.0 - position
            elif opponent_percent > effective_max_penalty_percent:
                # Decay from 0.0 to max penalty
                position = (opponent_percent - effective_zero_point) / (effective_max_penalty_percent - effective_zero_point)
                mismatch_multiplier = 0.0 - (position * abs(MISMATCH_MAX_PENALTY_VALUE))
            else:
                # Cap at maximum penalty
                mismatch_multiplier = MISMATCH_MAX_PENALTY_VALUE

    # Apply final modifiers to change
    if result == 1:
        change *= m_env
        change *= mismatch_multiplier  # Apply mismatch penalty to wins only
    elif result == 0:
        change += pity_bonus  # ADD pity points to loss calculation
    
    return min(max(RATING_FLOOR, team_points + change), RATING_CAP)

# =============================================================================
# === FORM & STATISTICS (TRENDS, HISTORY) ===
# =============================================================================

def _compute_form_from_recent(recent) -> Optional[Tuple[str, float, str]]:
    """
    Core form computation, shared by calculate_form() (which scans full
    history to build `recent`) and the incremental per-team deque tracker
    used during resimulation (which already maintains `recent` directly,
    with no history scan needed).

    Parameters:
    - recent: list of (side, match) tuples in chronological order
      (oldest first), already capped to the desired window size (e.g. 15).

    Returns: (grade, score, streak) or None if insufficient data
    """
    if len(recent) < 3:
        return None

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

    win_rate      = win_weighted / total_weight
    map_win_rate  = map_wins_weighted / map_total_weighted if map_total_weighted > 0 else 0.5
    comp_rate     = comp_weighted / total_weight

    def apply_form_compression(value: float, threshold: float = FORM_DIMINISHING_THRESHOLD, 
                                compression: float = FORM_DIMINISHING_COMPRESSION) -> float:
        if not FORM_DIMINISHING_ENABLED or value <= threshold:
            return value
        else:
            gain_above_threshold = value - threshold
            compressed_gain = gain_above_threshold * (1.0 - compression)
            result = threshold + compressed_gain
            return min(1.0, max(threshold, result))

    win_rate_compressed = apply_form_compression(win_rate)
    map_win_rate_compressed = apply_form_compression(map_win_rate)
    comp_rate_compressed = apply_form_compression(comp_rate)

    win_score  = win_rate_compressed * FORM_WIN_WEIGHT
    map_score  = map_win_rate_compressed * FORM_MAP_WEIGHT
    comp_score = comp_rate_compressed * FORM_COMP_WEIGHT

    base_score = win_score + map_score + comp_score

    # streak_chars already has exactly len(recent) entries (recent is
    # pre-capped to the desired window by the caller), so no further
    # slicing is needed here.
    streak = ''.join(streak_chars)
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


def calculate_form(team_name: str, n: int, history: List[Dict[str, Any]]) -> Optional[Tuple[str, float, str]]:
    """
    Calculate form rating across last n matches.

    Parameters:
    - team_name: The team to calculate form for
    - n: Number of recent matches to consider
    - history: List of match records (REQUIRED - no global fallback)

    Returns: (grade, score, streak) or None if insufficient data

    NOTE: This scans the full `history` list to find this team's matches,
    which is O(len(history)) per call. During resimulation (where this
    would otherwise be called twice per match against an ever-growing,
    freshly-sliced history — O(N^2) overall), use a FormTracker /
    _compute_form_from_recent directly against an incrementally
    maintained per-team deque instead. See _do_resimulation.
    """
    if not history:
        return None

    team_matches = []
    for m in history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team_name:
            team_matches.append(('t1', m))
        elif t2_name == team_name:
            team_matches.append(('t2', m))

    if not team_matches:
        return None

    if len(team_matches) < 3:
        return None

    return _compute_form_from_recent(team_matches[-n:])


def calculate_form_at_match_index(team_name: str, match_index: int, history: List[Dict[str, Any]]) -> Optional[Tuple[str, float, str]]:
    """
    Calculate form score for a team using only matches UP TO match_index.
    Used for historical form graphing.
    
    Parameters:
    - team_name: The team to calculate form for
    - match_index: Slice history up to this index (exclusive)
    - history: List of match records (REQUIRED - no global fallback)
    
    Returns: (grade, score, streak) or None if insufficient data
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
    
    def apply_form_compression(value: float, threshold: float = FORM_DIMINISHING_THRESHOLD, 
                                compression: float = FORM_DIMINISHING_COMPRESSION) -> float:
        if not FORM_DIMINISHING_ENABLED or value <= threshold:
            return value
        else:
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

def build_form_timeline(team_name, history=None):
    """
    Build a complete form timeline for a team across all their matches.
    Returns list of (date, form_score, grade, match_index) tuples.
    """
    if history is None:
        history = globals().get('history', [])
    
    # Get all matches for this team
    team_matches = []
    for idx, m in enumerate(history):
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team_name or t2_name == team_name:
            team_matches.append((idx, m))
    
    if len(team_matches) < 3:
        return None
    
    timeline = []
    
    # Calculate form at each match point (starting from match 4, since we need 3 prior)
    for i, (match_idx, m) in enumerate(team_matches):
        # Calculate form using history up to this match
        form = calculate_form_at_match_index(team_name, match_idx + 1, history)
        
        if form:
            grade, score, streak = form
            match_date = m.get('date', 'N/A')[:10]
            timeline.append((match_date, score, grade, match_idx))
    
    return timeline

def display_form_table() -> None:
    """Show form ratings for all teams with history, sorted by score."""
    if not history:
        print("\n>>> No match history available.")
        print("    Import matches to see form ratings.")
        return
    
    print("\n=== FORM RATINGS (Last 15 matches) ===")
    print(f"  {'#':<4} {'Team':<22} {'Gr':<4} {'Score':<7} {'Win':<7} {'Map':<7} {'Comp':<7} {'Streak'}")
    print(f"  {'':<4} {'':<22} {'':<4} {'':<7} {'(pts)':<7} {'(pts)':<7} {'(pts)':<7} {''}")
    print(f"  {'-'*76}")
    results = []
    for name in teams:
        form = calculate_form(name, n=15, history=history)
        if form:
            team_matches = []
            for m in history:
                t1_name = m.get('t1', {}).get('name')
                t2_name = m.get('t2', {}).get('name')
                if t1_name == name:
                    team_matches.append(('t1', m))
                elif t2_name == name:
                    team_matches.append(('t2', m))
            
            team_matches = sorted(team_matches, key=lambda x: x[1].get('date',''))[-15:]
            tw = ww = mw = mt = cw = 0.0
            for idx, (side, m) in enumerate(team_matches):
                t = m.get(side, {})
                opp = m.get('t2' if side == 't1' else 't1', {})
                
                matches_ago = len(team_matches) - 1 - idx
                exponent = 2.0
                normalized = matches_ago / 14
                r = 1.0 - (normalized ** exponent) * 0.85
                r = max(0.15, r)
                
                t_score = t.get('score', 0)
                opp_score = opp.get('score', 0)
                ww += (1.0 if t_score > opp_score else 0.0) * r
                mw += t_score * r
                mt += (t_score + opp_score) * r
                cw += (opp.get('pts_before', 500) / DIMINISHING_MAX) * r
                tw += r
            
            def apply_form_compression(value, threshold=FORM_DIMINISHING_THRESHOLD, 
                                        compression=FORM_DIMINISHING_COMPRESSION):
                if not FORM_DIMINISHING_ENABLED or value <= threshold:
                    return value
                else:
                    gain_above_threshold = value - threshold
                    compressed_gain = gain_above_threshold * (1.0 - compression)
                    result = threshold + compressed_gain
                    return min(1.0, max(threshold, result))

            win_rate_raw = ww/tw if tw else 0
            map_rate_raw = mw/mt if mt else 0
            comp_rate_raw = cw/tw if tw else 0

            win_rate_c = apply_form_compression(win_rate_raw)
            map_rate_c = apply_form_compression(map_rate_raw)
            comp_rate_c = apply_form_compression(comp_rate_raw)

            win_s  = round(win_rate_c * FORM_WIN_WEIGHT, 1) if tw else 0
            map_s  = round(map_rate_c * FORM_MAP_WEIGHT, 1) if mt else 0
            comp_s = round(comp_rate_c * FORM_COMP_WEIGHT, 1) if tw else 0
            
            grade, score, streak = form
            
            results.append((name, form, win_s, map_s, comp_s, streak))
    
    results.sort(key=lambda x: x[1][1], reverse=True)
    for i, (name, (grade, score, streak), win_s, map_s, comp_s, streak_display) in enumerate(results, 1):
        n = min(15, sum(1 for m in history if m.get('t1', {}).get('name') == name or m.get('t2', {}).get('name') == name))
        print(f"  {i:<4} {name:<22} {grade:<4} {score:<7} {win_s:<7} {map_s:<7} {comp_s:<7} {streak_display} ({n})")
    if not results:
        print("  [!] No teams have enough match history for form calculation (minimum 3 matches).")
        
def analyze_team_form() -> None:
    """
    Detailed breakdown and analysis of a team's form score.
    """
    print("\n=== ANALYZE TEAM FORM ===")
    
    team_raw = check_cmd(input("Enter team name: ")).strip()
    if get_cmd(team_raw) in ['back', '0']:
        return
    
    team = find_team(team_raw)
    if not team:
        print(">>> Team not found.")
        return
    
    form = calculate_form(team, n=15, history=history)
    if not form:
        print(f">>> Not enough match history for {team} (minimum 3 matches required).")
        return
    
    grade, score, streak = form
    
    team_matches = []
    for m in history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team:
            team_matches.append(('t1', m))
        elif t2_name == team:
            team_matches.append(('t2', m))
    
    if not team_matches:
        print(">>> No matches found for this team.")
        return
    
    team_matches = sorted(team_matches, key=lambda x: x[1].get('date', ''))
    recent = team_matches[-15:]
    
    streak_bonus = 0.0
    consecutive_losses = 0
    total_wins = 0
    total_losses = 0

    streak_for_bonus = []
    for idx, (side, m) in enumerate(recent):
        t = m.get(side, {})
        opp = m.get('t2' if side == 't1' else 't1', {})
        won = t.get('score', 0) > opp.get('score', 0)
        streak_for_bonus.append('W' if won else 'L')
    
    streak_full = ''.join(streak_for_bonus)

    if FORM_STREAK_BONUS_ENABLED:
        for result in streak_full:
            if result == 'W':
                streak_bonus += FORM_STREAK_BONUS_PER_WIN
                streak_bonus = min(FORM_STREAK_BONUS_MAX, streak_bonus)
                total_wins += 1
                consecutive_losses = 0
            else:
                consecutive_losses += 1
                total_losses += 1
                if consecutive_losses >= FORM_STREAK_LOSS_RESET_COUNT:
                    streak_bonus = 0.0
                    consecutive_losses = 0
                else:
                    streak_bonus *= 0.5
        
        streak_bonus = min(FORM_STREAK_BONUS_MAX, streak_bonus)
    
    base_score = score - streak_bonus
    
    print(f"\n=== {team} - Form Analysis ===")
    total_form_points = FORM_WIN_WEIGHT + FORM_MAP_WEIGHT + FORM_COMP_WEIGHT
    print(f"Form: {grade} ({score}/{total_form_points:.1f})  |  Streak: {streak}  |  Matches: {len(recent)}")
    
    if streak_bonus > 0:
        print(f"      +-- Win Streak Bonus: +{streak_bonus:.1f} ({total_wins}W/{total_losses}L in window) [Base: {base_score:.1f}]")
    elif total_losses >= FORM_STREAK_LOSS_RESET_COUNT and consecutive_losses >= FORM_STREAK_LOSS_RESET_COUNT:
        print(f"      +-- Streak Bonus Wiped ({consecutive_losses} Consecutive Losses) [Base: {base_score:.1f}]")
    else:
        print(f"      +-- No Active Bonus [Base: {base_score:.1f}]")
    
    total_weight = 0.0
    win_weighted = 0.0
    map_wins_weighted = 0.0
    map_total_weighted = 0.0
    comp_weighted = 0.0
    
    team_rating = teams.get(team, 1000)
    threshold = team_rating * 0.90
    buffer = team_rating * 0.10
    
    raw_match_data = []
    
    for idx, (side, m) in enumerate(recent):
        t = m.get(side, {})
        opp_side = 't2' if side == 't1' else 't1'
        opp = m.get(opp_side, {})
        
        t_score = t.get('score', 0)
        opp_score = opp.get('score', 0)
        opp_pts = opp.get('pts_before', 500)
        won = t_score > opp_score
        
        matches_ago = len(recent) - 1 - idx
        exponent = 2.0
        normalized = matches_ago / 14
        recency = 1.0 - (normalized ** exponent) * 0.85
        recency = max(0.15, recency)
        
        win_weighted += (1.0 if won else 0.0) * recency
        map_wins_weighted += t_score * recency
        map_total_weighted += (t_score + opp_score) * recency
        
        if opp_pts >= threshold:
            comp_value = 1.0
        else:
            buffered_rating = opp_pts + buffer
            comp_value = min(1.0, max(0.0, buffered_rating / DIMINISHING_MAX))
        
        comp_weighted += comp_value * recency
        total_weight += recency
        
        raw_match_data.append({
            'date': m.get('date', 'N/A')[:10],
            'opponent': opp.get('name', 'Unknown'),
            'score': f"{t_score}-{opp_score}",
            'result': 'W' if won else 'L',
            'opp_rating': opp_pts,
            'recency': recency,
            'won': won,
            't_score': t_score,
            'opp_score': opp_score,
            'comp_value': comp_value
        })
    
    win_rate = win_weighted / total_weight if total_weight > 0 else 0.0
    map_win_rate = map_wins_weighted / map_total_weighted if map_total_weighted > 0 else 0.0
    comp_rate = min(1.0, max(0.0, comp_weighted / total_weight)) if total_weight > 0 else 0.0
    
    def apply_form_compression(value: float, threshold: float = FORM_DIMINISHING_THRESHOLD, 
                                compression: float = FORM_DIMINISHING_COMPRESSION) -> float:
        if not FORM_DIMINISHING_ENABLED or value <= threshold:
            return value
        else:
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
    
    match_details = []
    for raw in raw_match_data:
        win_contrib = (1.0 if raw['won'] else 0.0) * raw['recency'] * FORM_WIN_WEIGHT / total_weight if total_weight > 0 else 0
        map_contrib = (raw['t_score'] / (raw['t_score'] + raw['opp_score']) if (raw['t_score'] + raw['opp_score']) > 0 else 0) * raw['recency'] * FORM_MAP_WEIGHT / total_weight if total_weight > 0 else 0
        comp_contrib = raw['comp_value'] * raw['recency'] * FORM_COMP_WEIGHT / total_weight if total_weight > 0 else 0
        match_form = win_contrib + map_contrib + comp_contrib
        
        match_details.append({
            'date': raw['date'],
            'opponent': raw['opponent'],
            'score': raw['score'],
            'result': raw['result'],
            'recency': raw['recency'],
            'match_form': match_form
        })
    
    print(f"\nComponents:")
    
    win_pct = (win_rate * 100) if total_weight > 0 else 0
    win_pct_compressed = (win_rate_compressed * 100) if total_weight > 0 else 0
    win_bar_len = int((win_score / FORM_WIN_WEIGHT) * 30) if FORM_WIN_WEIGHT > 0 else 0
    win_bar = '#' * win_bar_len + '-' * (30 - win_bar_len)
    if FORM_DIMINISHING_ENABLED and win_rate > FORM_DIMINISHING_THRESHOLD:
        print(f"  Win Rate              {win_score:<5.1f}  {win_bar} ({win_pct:.0f}%->{win_pct_compressed:.0f}%)")
    else:
        print(f"  Win Rate              {win_score:<5.1f}  {win_bar} ({win_pct:.0f}%)")
    
    map_pct = (map_win_rate * 100) if map_total_weighted > 0 else 0
    map_pct_compressed = (map_win_rate_compressed * 100) if map_total_weighted > 0 else 0
    map_bar_len = int((map_score / FORM_MAP_WEIGHT) * 30) if FORM_MAP_WEIGHT > 0 else 0
    map_bar = '#' * map_bar_len + '-' * (30 - map_bar_len)
    if FORM_DIMINISHING_ENABLED and map_win_rate > FORM_DIMINISHING_THRESHOLD:
        print(f"  Map Win Rate          {map_score:<5.1f}  {map_bar} ({map_pct:.0f}%->{map_pct_compressed:.0f}%)")
    else:
        print(f"  Map Win Rate          {map_score:<5.1f}  {map_bar} ({map_pct:.0f}%)")
    
    comp_pct = (comp_rate * 100) if total_weight > 0 else 0
    comp_pct_compressed = (comp_rate_compressed * 100) if total_weight > 0 else 0
    comp_bar_len = int((comp_score / FORM_COMP_WEIGHT) * 30) if FORM_COMP_WEIGHT > 0 else 0
    comp_bar = '#' * comp_bar_len + '-' * (30 - comp_bar_len)
    if FORM_DIMINISHING_ENABLED and comp_rate > FORM_DIMINISHING_THRESHOLD:
        print(f"  Competition Strength  {comp_score:<5.1f}  {comp_bar} ({comp_pct:.0f}%->{comp_pct_compressed:.0f}%)")
    else:
        print(f"  Competition Strength  {comp_score:<5.1f}  {comp_bar} ({comp_pct:.0f}%)")
    
    print(f"\nRecent Matches:")
    print(f"  {'#':<3} {'Date':<8} {'Opponent':<18} {'Score':<6} {'Form Points':>11}")
    print(f"  {'-'*50}")
    
    for idx, details in enumerate(match_details, 1):
        date_short = details['date'][5:]
        result_icon = 'W' if details['result'] == 'W' else 'L'
        print(f"  {idx:<3} {date_short:<8} {details['opponent'][:18]:<18} {details['score']:<6} {result_icon}  {details['match_form']:>6.1f}")
    
    print(f"\nTrend:", end="")
    trend_data = []
    for window in [5, 10, 15]:
        if len(recent) >= window:
            window_matches = recent[-window:]
            w_w = w_mw = w_mt = w_c = w_t = 0.0
            
            for idx, (side, m) in enumerate(window_matches):
                t = m.get(side, {})
                opp = m.get('t2' if side == 't1' else 't1', {})
                
                matches_ago = len(window_matches) - 1 - idx
                exponent = 2.0
                normalized = matches_ago / 14
                r = 1.0 - (normalized ** exponent) * 0.85
                r = max(0.15, r)
                
                won = t.get('score', 0) > opp.get('score', 0)
                w_w += (1.0 if won else 0.0) * r
                w_mw += t.get('score', 0) * r
                w_mt += (t.get('score', 0) + opp.get('score', 0)) * r
                
                opp_pts = opp.get('pts_before', 500)
                if opp_pts >= threshold:
                    comp_value = 1.0
                else:
                    buffered_rating = opp_pts + buffer
                    comp_value = min(1.0, max(0.0, buffered_rating / DIMINISHING_MAX))
                
                w_c += comp_value * r
                w_t += r
            
            w_rate = w_w / w_t if w_t > 0 else 0
            w_map = w_mw / w_mt if w_mt > 0 else 0
            w_comp = w_c / w_t if w_t > 0 else 0
            w_score = round((w_rate * FORM_WIN_WEIGHT) + (w_map * FORM_MAP_WEIGHT) + (w_comp * FORM_COMP_WEIGHT), 1)
            
            trend_arrow = "^" if w_score > score else ("v" if w_score < score else "-")
            diff = f"+{w_score - score:.1f}" if w_score > score else f"{w_score - score:.1f}"
            trend_data.append((window, w_score, trend_arrow, diff))
    
    for window, w_score, trend_arrow, diff in trend_data:
        print(f"  Last {window} ({w_score} {trend_arrow} {diff})", end="")
    print()
    
    insights = []
    
    win_rate_pct = win_rate * 100
    if win_rate_pct >= 80:
        insights.append(f"Dominating matches ({win_rate_pct:.0f}%)")
    elif win_rate_pct >= 60:
        insights.append(f"Consistent in matches ({win_rate_pct:.0f}%)")
    elif win_rate_pct >= 40:
        insights.append(f"Inconsistent in matches ({win_rate_pct:.0f}%)")
    else:
        insights.append(f"Poor performance in matches ({win_rate_pct:.0f}%)")
    
    map_pct = map_win_rate * 100
    if map_pct >= 70:
        insights.append(f"Dominating maps ({map_pct:.0f}%)")
    elif map_pct >= 55:
        insights.append(f"Highly competitive on maps ({map_pct:.0f}%)")
    elif map_pct >= 45:
        insights.append(f"Competitive on maps ({map_pct:.0f}%)")
    else:
        insights.append(f"Poor performance on maps ({map_pct:.0f}%)")
    
    if comp_rate >= 0.90:
        insights.append("Great competition")
    elif comp_rate >= 0.75:
        insights.append("Okay competition")
    else:
        insights.append("Farming bad teams")
    
    consecutive = 1
    for i in range(len(streak) - 2, -1, -1):
        if streak[i] == streak[-1]:
            consecutive += 1
        else:
            break
    
    if streak.endswith('W'):
        if consecutive >= 10:
            insights.append(f"{consecutive}-Match legendary win streak")
        elif consecutive >= 5:
            insights.append(f"{consecutive}-Match hot win streak")
        elif consecutive >= 3:
            insights.append(f"{consecutive}-Match small win streak")
    elif streak.endswith('L'):
        if consecutive >= 10:
            insights.append(f"{consecutive}-Match rebuild needed")
        elif consecutive >= 5:
            insights.append(f"{consecutive}-Match crisis mode")
        elif consecutive >= 3:
            insights.append(f"{consecutive}-Match rough patch")
    
    if len(trend_data) > 0:
        latest_window, latest_score, trend_arrow, diff = trend_data[0]
        diff_val = float(diff)
        if diff_val >= 5.0:
            insights.append(f"Form hiking {trend_arrow}{trend_arrow}")
        elif diff_val >= 2.0:
            insights.append(f"Form improving {trend_arrow}")
        elif diff_val <= -5.0:
            insights.append(f"Form slumping {trend_arrow}{trend_arrow}")
        elif diff_val <= -2.0:
            insights.append(f"Form declining {trend_arrow}")
    
    print(f"\nInsights:  {' | '.join(insights)}")
    print(f"\n{'='*50}")
    print("\nPress Enter to return...")
    input()

def get_team_trend(team_name, days=30):
    """
    Calculate rating trend for a team over the last N days.
    Returns (trend_type, points_change) where trend_type is:
    -- (stable), ↑ (slight up), ↑↑ (strong up), ↓ (slight down), ↓↓ (strong down)
    """
    from datetime import datetime, timedelta
    
    cutoff_date = datetime.now() - timedelta(days=days)
    
    team_ratings = []
    for m in history:
        t1_data = m.get('t1', {})
        t2_data = m.get('t2', {})
        match_date_str = m.get('date', 'N/A')
        
        if match_date_str == 'N/A' or not match_date_str:
            continue
        
        try:
            match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d")
            if match_date < cutoff_date:
                continue
        except:
            continue
        
        if t1_data.get('name') == team_name:
            pts_after = t1_data.get('pts_after')
            if pts_after is not None:
                team_ratings.append((match_date, pts_after))
        elif t2_data.get('name') == team_name:
            pts_after = t2_data.get('pts_after')
            if pts_after is not None:
                team_ratings.append((match_date, pts_after))
    
    if len(team_ratings) < 2:
        return ('--', 0)
    
    # Sort by date
    team_ratings.sort(key=lambda x: x[0])
    
    start_rating = team_ratings[0][1]
    end_rating = team_ratings[-1][1]
    points_diff = end_rating - start_rating
    
    # Determine arrow type based on thresholds
    if points_diff >= 50:
        trend = '↑↑'
    elif points_diff >= 25:
        trend = '↑'
    elif points_diff <= -50:
        trend = '↓↓'
    elif points_diff <= -25:
        trend = '↓'
    else:
        trend = '--'
    
    return (trend, points_diff)


def display_rankings():
    """
    Print current team rankings with 30-day trends and depreciated ratings.
    Teams inactive {DEPRECIATION_THRESHOLD}+ days show their current depreciated rating.
    Teams inactive {INACTIVE_ARCHIVE_DAYS}+ days are moved to a separate Archived
    section (see is_team_archived) instead of cluttering the main table forever.
    """
    print("\n=== CURRENT RANKINGS ===")
    print(f"  {'#':<4} {'Team':<26} {'Rating':<10} {'30D Trend':<12} {'Depreciation Punishment'}")
    print(f"  {'-'*81}")
    
    today = datetime.now().date()
    
    # === FIX: Calculate depreciated ratings FIRST, then sort ===
    team_ratings = []
    provisional_list = []
    archived_list = []
    date_index = build_match_date_index(history)
    for name, points in teams.items():
        if is_provisional(name):
            matches_done = provisional_teams.get(name, 0)
            provisional_list.append((name, int(points), matches_done))
            continue

        last_match = get_team_last_match_date_before(name, index=date_index)
        has_last_match = last_match is not None
        display_rating = int(points)
        dep_loss = 0
        days_inactive = 0
        
        if last_match:
            days_inactive = (today - last_match.date()).days
            if days_inactive > DEPRECIATION_THRESHOLD:
                depreciated = calculate_depreciation(points, days_inactive, name)
                dep_loss = points - depreciated
                display_rating = int(depreciated)

            if days_inactive > INACTIVE_ARCHIVE_DAYS:
                archived_list.append((name, display_rating, days_inactive))
                continue
        
        team_ratings.append((name, points, display_rating, dep_loss, days_inactive, has_last_match))
    
    # === FIX: Sort by display_rating (depreciated) instead of raw points ===
    team_ratings.sort(key=lambda x: x[2], reverse=True)
    
    for i, (name, points, display_rating, dep_loss, days_inactive, has_last_match) in enumerate(team_ratings, 1):
        # Get 30-day trend
        trend, diff = get_team_trend(name, days=30)
        
        # Build depreciation string
        if days_inactive > DEPRECIATION_THRESHOLD:
            dep_str = f"-{int(dep_loss):>3} pts ({days_inactive:>2}d)"
        elif has_last_match:
            dep_str = f"{'':>8} ({days_inactive:>2}d)"
        else:
            dep_str = f"{'':>8} (--)"
        
        print(f"  {i:<4} {name:<26} {display_rating:<10} {trend:<12} {dep_str}")
    
    print(f"  {'-'*81}")
    print(f"  Trend: ↑↑ (+50)  ↑ (+25)  -- (±24)  ↓ (-25)  ↓↓ (-50)")
    print(f"  Rating shown is depreciated value for teams inactive {DEPRECIATION_THRESHOLD}+ days")

    if provisional_list:
        print(f"\n  --- Provisional Teams (excluded from rankings) ---")
        for name, rating, matches_done in sorted(provisional_list, key=lambda x: x[1], reverse=True):
            remaining = PROVISIONAL_MATCH_THRESHOLD - matches_done
            print(f"  {'[P]':<4} {name:<26} {rating:<10} {matches_done}/{PROVISIONAL_MATCH_THRESHOLD} matches  ({remaining} to establish)")

    if archived_list:
        print(f"\n  --- Archived Teams (inactive {INACTIVE_ARCHIVE_DAYS}+ days, excluded from rankings) ---")
        for name, rating, days_inactive in sorted(archived_list, key=lambda x: x[2]):
            print(f"  {'[A]':<4} {name:<26} {rating:<10} {days_inactive}d inactive")
        print(f"  (Archived teams reappear automatically the moment they play a new match.")
        print(f"   See Team Management > Archived / Inactive Teams to review or delete them.)")

def display_elite_teams_over_time() -> None:
    """
    Display rating progression of ALL teams that reached ELITE_THRESHOLD+ CSRS Elo.
    """
    from datetime import datetime, timedelta
    
    if len(history) < 5:
        print("\n  Not enough match history to generate trend (minimum 5 matches required).")
        return
    
    start_date, end_date = pick_date_range()
    if start_date is None or end_date is None:
        print("\n  >>> Date selection cancelled.")
        return
    
    sorted_hist = sorted(history, key=lambda m: (m.get('date') == 'N/A', m.get('date', '')))
    
    team_peaks_in_range = {}
    
    for m in sorted_hist:
        match_date_str = m.get('date', 'N/A')
        if match_date_str == 'N/A' or not match_date_str:
            continue
        try:
            match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d").date()
        except:
            continue
        
        if not (start_date <= match_date <= end_date):
            continue
        
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if not t1_name or not t2_name:
            continue
        
        t1_pts = m.get('t1', {}).get('pts_after')
        t2_pts = m.get('t2', {}).get('pts_after')
        
        if t1_pts is not None:
            if t1_name not in team_peaks_in_range or t1_pts > team_peaks_in_range[t1_name]:
                team_peaks_in_range[t1_name] = t1_pts
        if t2_pts is not None:
            if t2_name not in team_peaks_in_range or t2_pts > team_peaks_in_range[t2_name]:
                team_peaks_in_range[t2_name] = t2_pts
    
    elite_team_names = set([name for name, peak in team_peaks_in_range.items() if peak >= ELITE_THRESHOLD])
    print(f"\n  Elite Teams (reached {ELITE_THRESHOLD}+ in date range): {len(elite_team_names)}")
    
    team_history = {team: [] for team in elite_team_names}
    all_dates = set()
    
    first_match_date = None
    first_match_in_range = None
    for m in sorted_hist:
        match_date_str = m.get('date', 'N/A')
        if match_date_str == 'N/A' or not match_date_str:
            continue
        try:
            match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d").date()
            if match_date >= start_date:
                first_match_date = match_date
                first_match_in_range = m
                break
        except:
            continue
    
    start_point_date_str = (first_match_date - timedelta(days=1)).strftime("%Y-%m-%d") if first_match_date else start_date.strftime("%Y-%m-%d")
    
    starting_ratings = {}
    if first_match_in_range:
        t1_name = first_match_in_range.get('t1', {}).get('name')
        t2_name = first_match_in_range.get('t2', {}).get('name')
        t1_pts_before = first_match_in_range.get('t1', {}).get('pts_before')
        t2_pts_before = first_match_in_range.get('t2', {}).get('pts_before')
        
        if t1_name and t1_name in elite_team_names and t1_pts_before is not None:
            starting_ratings[t1_name] = t1_pts_before
        if t2_name and t2_name in elite_team_names and t2_pts_before is not None:
            starting_ratings[t2_name] = t2_pts_before
    
    for m in sorted_hist:
        match_date_str = m.get('date', 'N/A')
        if match_date_str == 'N/A' or not match_date_str:
            continue
        try:
            match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d").date()
        except:
            continue
        if match_date < start_date or match_date > end_date:
            continue
        
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        
        if t1_name and t1_name in elite_team_names and t1_name not in starting_ratings:
            pts_before = m.get('t1', {}).get('pts_before')
            if pts_before is not None:
                starting_ratings[t1_name] = pts_before
        
        if t2_name and t2_name in elite_team_names and t2_name not in starting_ratings:
            pts_before = m.get('t2', {}).get('pts_before')
            if pts_before is not None:
                starting_ratings[t2_name] = pts_before
    
    for team_name in elite_team_names:
        start_rating = starting_ratings.get(team_name, teams.get(team_name, 1000))
        team_history[team_name].append((start_point_date_str, start_rating))
        all_dates.add(start_point_date_str)
    
    for m in sorted_hist:
        match_date_str = m.get('date', 'N/A')
        if match_date_str == 'N/A' or not match_date_str:
            continue
        try:
            match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d").date()
        except:
            continue
        if match_date < start_date or match_date > end_date:
            continue
        
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if not t1_name or not t2_name:
            continue
        
        date_key = match_date_str[:10]
        all_dates.add(date_key)
        
        t1_pts = m.get('t1', {}).get('pts_after')
        t2_pts = m.get('t2', {}).get('pts_after')
        
        if t1_name in elite_team_names and t1_pts is not None:
            team_history[t1_name].append((date_key, t1_pts))
        if t2_name in elite_team_names and t2_pts is not None:
            team_history[t2_name].append((date_key, t2_pts))
    
    end_date_str = end_date.strftime("%Y-%m-%d")
    all_dates.add(end_date_str)
    
    elite_teams = [team for team, matches in team_history.items() if len(matches) >= 2]
    if len(elite_teams) < 2:
        print("\n  Not enough elite team matches in selected date range.")
        return
    
    all_dates = sorted(all_dates)
    team_initial_ratings = {team: matches[0][1] if matches else 1000 for team, matches in team_history.items()}
    
    print(f"\n  [OK] Found {len(all_dates)} unique dates (including start point), {len(elite_teams)} elite teams")
    
    # === PRE-CALCULATE FORM DATA (Performance Fix) ===
    team_form_cache = {}
    for team in elite_teams:
        team_form_cache[team] = calculate_form(team, n=15, history=history)
    
    try:
        _show_elite_teams_graph(team_history, all_dates, elite_teams, teams, team_initial_ratings, team_peaks_in_range, start_date, end_date, start_point_date_str, team_form_cache)
    except Exception as e:
        logger.error(f"Graph failed: {e}")
        import traceback
        traceback.print_exc()
        _display_elite_teams_text(team_history, all_dates, elite_teams, teams, team_initial_ratings, start_date, end_date)

def _show_elite_teams_graph(team_history: Dict[str, List[Tuple[str, float]]], all_dates: List[str], elite_teams: List[str], final_ratings: Dict[str, float], initial_ratings: Dict[str, float], team_peaks: Dict[str, float], start_date: datetime, end_date: datetime, start_point_date_str: str, team_form_cache: Dict[str, Optional[Tuple[str, float, str]]]) -> None:
    """
    Create tkinter window with line graph for elite teams.
    FIXED: Transition markers at day-before-match and depreciation-to-match transitions.
    
    Parameters:
    - team_form_cache: Pre-calculated form data to avoid recalculation during render
    """
    import tkinter as tk
    from datetime import datetime, timedelta
    import re
    
    root = tk.Tk()
    root.title("ELITE TEAMS OVER TIME")
    root.configure(bg='#1a1a1a')
    
    if len(all_dates) > 1:
        date_objects = [datetime.strptime(d, "%Y-%m-%d") for d in all_dates]
        total_days = (date_objects[-1] - date_objects[0]).days + 1
    else:
        total_days = 1
    
    max_rating_in_range = ELITE_THRESHOLD
    for team_name in elite_teams:
        matches = team_history.get(team_name, [])
        for date_key, rating in matches:
            if rating > max_rating_in_range:
                max_rating_in_range = rating
    
    max_y = ((int(max_rating_in_range) // 50) + 1) * 50
    if max_y > RATING_CAP:
        max_y = RATING_CAP
    
    team_actual_finals = {}
    for team in elite_teams:
        matches = team_history.get(team, [])
        if matches:
            team_actual_finals[team] = matches[-1][1]
        else:
            team_actual_finals[team] = final_ratings.get(team, 1000)
    
    sorted_teams = sorted(elite_teams, key=lambda t: team_actual_finals[t], reverse=True)
    
    graph_data = {
        'total_days': total_days, 'all_dates': all_dates, 'elite_teams': elite_teams,
        'team_history': team_history, 'initial_ratings': initial_ratings,
        'final_ratings': final_ratings, 'team_peaks': team_peaks,
        'start_date': start_date, 'end_date': end_date,
        'start_point_date_str': start_point_date_str, 'min_y': ELITE_THRESHOLD, 'max_y': max_y,
        'teams_by_rating': sorted_teams,
        'team_actual_finals': team_actual_finals,
        'team_form_cache': team_form_cache,
    }
    
    team_colors = ['#FFD700', '#C0C0C0', '#CD7F32', '#e6194B', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#fabed4', '#469990', '#dcbeff', '#9A6324', '#fffac8', '#800000', '#aaffc3', '#808000', '#ffd8b1']
    graph_data['team_color_map'] = {team_name: team_colors[i % len(team_colors)] for i, team_name in enumerate(sorted_teams)}
    
    width, height = 1600, 533
    root.geometry(f"{width}x{height}")
    
    canvas = tk.Canvas(root, bg='#1a1a1a', highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    close_btn = [None]
    
    def on_resize(event):
        try:
            canvas.configure(scrollregion=(0, 0, event.width, event.height))
            draw_graph(event.width, event.height)
            if close_btn[0]: close_btn[0].place(x=event.width//2 - 40, y=event.height - 50)
        except Exception as e:
            logger.error(f"Resize error: {e}")
    
    canvas.bind("<Configure>", on_resize)
    
    def draw_graph(canvas_width: int, canvas_height: int) -> None:
        def dim_color(color: str, factor: float = 0) -> str:
            match = re.match(r'#([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})', color)
            if match:
                r = int(match.group(1), 16)
                g = int(match.group(2), 16)
                b = int(match.group(3), 16)
                bg_r, bg_g, bg_b = 26, 26, 26
                new_r = int(r * factor + bg_r * (1 - factor))
                new_g = int(g * factor + bg_g * (1 - factor))
                new_b = int(b * factor + bg_b * (1 - factor))
                return f'#{new_r:02X}{new_g:02X}{new_b:02X}'
            return color
        
        def draw_split_line(x1: float, y1: float, rating1: float, x2: float, y2: float, rating2: float, color: str, width_above: int = 2, width_below: int = 1) -> None:
            if rating1 >= ELITE_THRESHOLD and rating2 >= ELITE_THRESHOLD:
                canvas.create_line(x1, y1, x2, y2, fill=color, width=width_above)
                return
            if rating1 < ELITE_THRESHOLD and rating2 < ELITE_THRESHOLD:
                canvas.create_line(x1, y1, x2, y2, fill=dim_color(color, 0), width=width_below)
                return
            if abs(rating2 - rating1) > 0.01:
                t = (ELITE_THRESHOLD - rating1) / (rating2 - rating1)
                x_cross = x1 + t * (x2 - x1)
                y_cross = y_elite
                if rating1 >= ELITE_THRESHOLD:
                    canvas.create_line(x1, y1, x_cross, y_cross, fill=color, width=width_above)
                    canvas.create_line(x_cross, y_cross, x2, y2, fill=dim_color(color, 0), width=width_below)
                else:
                    canvas.create_line(x1, y1, x_cross, y_cross, fill=dim_color(color, 0), width=width_below)
                    canvas.create_line(x_cross, y_cross, x2, y2, fill=color, width=width_above)
            else:
                canvas.create_line(x1, y1, x2, y2, fill=color if rating1 >= ELITE_THRESHOLD else dim_color(color, 0), width=width_above if rating1 >= ELITE_THRESHOLD else width_below)
        
        try:
            canvas.delete("all")
            margin_left, margin_top, margin_bottom = 53, 47, 130
            graph_width = int((canvas_width - margin_left) * 0.60)
            graph_right_edge = margin_left + graph_width
            graph_height = max(100, canvas_height - margin_top - margin_bottom)
            pixels_per_day = graph_width / graph_data['total_days'] if graph_data['total_days'] > 1 else graph_width
            min_y = graph_data['min_y']
            max_y = graph_data['max_y']
            range_y = max_y - min_y
            y_elite = margin_top + graph_height
            
            canvas.create_text(canvas_width//2, 17, text="ELITE TEAMS OVER TIME", font=('Arial', 12, 'bold'), fill='white')
            canvas.create_text(margin_left, 30, text=f"{graph_data['start_date']} to {graph_data['end_date']}  |  {min_y}-{max_y} CSRS  |  {graph_data['total_days']} days", font=('Arial', 7), fill='#888888', anchor='w')
            
            date_to_x = {}
            if len(graph_data['all_dates']) > 1:
                date_objects = [datetime.strptime(d, "%Y-%m-%d") for d in graph_data['all_dates']]
                start_dt = date_objects[0]
                for i, dt in enumerate(date_objects):
                    days_from_start = (dt - start_dt).days
                    date_to_x[graph_data['all_dates'][i]] = margin_left + (days_from_start * pixels_per_day)
            else:
                date_to_x[graph_data['all_dates'][0]] = margin_left + graph_width // 2
            
            end_date_str = graph_data['all_dates'][-1] if graph_data['all_dates'] else None
            end_x = date_to_x.get(end_date_str, graph_right_edge) if end_date_str else graph_right_edge
            
            num_lines = 6
            for i in range(num_lines):
                y_val = min_y + (i * (range_y / (num_lines - 1)))
                y_pos = margin_top + graph_height - (i / (num_lines - 1) * graph_height)
                canvas.create_line(margin_left, y_pos, end_x, y_pos, fill='#333333', dash=(5, 5))
                canvas.create_text(margin_left - 6, y_pos, text=str(int(y_val)), font=('Arial', 6), fill='#888888', anchor='e')
            
            canvas.create_line(margin_left, margin_top, margin_left, y_elite, fill='white', width=1)
            canvas.create_line(margin_left, y_elite, end_x, y_elite, fill='white', width=1)
            
            team_points, declined_teams, team_display_ratings = {}, set(), {}
            
            for team_name in graph_data['teams_by_rating']:
                color = graph_data['team_color_map'][team_name]
                points = []
                matches = graph_data['team_history'].get(team_name, [])
                actual_final = graph_data['team_actual_finals'][team_name]
                
                if actual_final < ELITE_THRESHOLD:
                    declined_teams.add(team_name)
                
                for date_key, rating in matches:
                    if date_key in date_to_x:
                        x = date_to_x[date_key]
                        y = margin_top + graph_height - ((rating - min_y) / range_y * graph_height)
                        points.append((x, y, date_key, rating, 'match'))
                
                points.sort(key=lambda p: p[0])
                
                if len(points) > 1:
                    unique_points = [points[0]]
                    for p in points[1:]:
                        if abs(p[0] - unique_points[-1][0]) < 1:
                            unique_points[-1] = p
                        else:
                            unique_points.append(p)
                    points = unique_points
                
                current_rating = points[-1][3] if points else actual_final
                last_match_date = points[-1][2] if points else None
                
                depreciated_rating = current_rating
                days_inactive = 0
                
                if last_match_date and end_date_str:
                    try:
                        end_date_obj = datetime.strptime(end_date_str, "%Y-%m-%d")
                        last_date_obj = datetime.strptime(last_match_date, "%Y-%m-%d")
                        days_inactive = (end_date_obj - last_date_obj).days
                        if days_inactive > DEPRECIATION_THRESHOLD:
                            depreciated_rating = calculate_depreciation(current_rating, days_inactive, team_name)
                    except:
                        pass
                
                team_display_ratings[team_name] = depreciated_rating if days_inactive > DEPRECIATION_THRESHOLD else current_rating
                
                if points and end_date_str:
                    y_end = margin_top + graph_height - ((depreciated_rating - min_y) / range_y * graph_height)
                    points.append((end_x, y_end, end_date_str, depreciated_rating, 'end'))
                
                if len(points) > 1:
                    for i in range(len(points) - 1):
                        x1, y1, rating1, date1, point_type1 = points[i][0], points[i][1], points[i][3], points[i][2], points[i][4]
                        x2, y2, rating2, date2, point_type2 = points[i+1][0], points[i+1][1], points[i+1][3], points[i+1][2], points[i+1][4]
                        
                        try:
                            date1_obj = datetime.strptime(date1, "%Y-%m-%d")
                            date2_obj = datetime.strptime(date2, "%Y-%m-%d")
                            days_gap = (date2_obj - date1_obj).days
                        except:
                            days_gap = 0
                        
                        is_end_segment = (point_type2 == 'end')
                        has_depreciation = days_gap > DEPRECIATION_THRESHOLD

                        if has_depreciation:
                            x_day7 = x1 + ((x2 - x1) * (DEPRECIATION_THRESHOLD / days_gap))
                            y_day7 = y1
                            draw_split_line(x1, y1, rating1, x_day7, y_day7, rating1, color)
                            
                            dep_points = [(x_day7, y_day7, rating1)]
                            dep_end_day = days_gap if is_end_segment else days_gap - 1
                            
                            if dep_end_day > DEPRECIATION_THRESHOLD:
                                for day in range(DEPRECIATION_THRESHOLD + 1, dep_end_day + 1):
                                    daily_rating = calculate_depreciation(rating1, day, team_name)
                                    
                                    day_x = x1 + ((x2 - x1) * (day / days_gap))
                                    daily_y = margin_top + graph_height - ((daily_rating - min_y) / range_y * graph_height)
                                    dep_points.append((day_x, daily_y, daily_rating))
                                
                                for j in range(len(dep_points) - 1):
                                    dot1 = dep_points[j]
                                    dot2 = dep_points[j + 1]
                                    draw_split_line(dot1[0], dot1[1], dot1[2], dot2[0], dot2[1], dot2[2], color)
                            
                            if not is_end_segment:
                                draw_split_line(dep_points[-1][0], dep_points[-1][1], dep_points[-1][2], x2, y2, rating2, color)
                                
                                trans_x, trans_y, trans_rating = dep_points[-1]
                                marker_fill = color if trans_rating >= ELITE_THRESHOLD else dim_color(color, 0)
                                outline_color = 'white' if trans_rating >= ELITE_THRESHOLD else ''
                                canvas.create_oval(trans_x-3, trans_y-3, trans_x+3, trans_y+3, fill=marker_fill, outline=outline_color, width=1)
                        else:
                            if days_gap >= 1:
                                if days_gap == 1:
                                    draw_split_line(x1, y1, rating1, x2, y2, rating2, color)
                                else:
                                    x_flat_end = x1 + ((x2 - x1) * ((days_gap - 1) / days_gap))
                                    draw_split_line(x1, y1, rating1, x_flat_end, y1, rating1, color)
                                    draw_split_line(x_flat_end, y1, rating1, x2, y2, rating2, color)
                                    
                                    if not is_end_segment and abs(x_flat_end - x1) > 5:
                                        marker_fill = color if rating1 >= ELITE_THRESHOLD else dim_color(color, 0)
                                        outline_color = 'white' if rating1 >= ELITE_THRESHOLD else ''
                                        canvas.create_oval(x_flat_end-3, y1-3, x_flat_end+3, y1+3, fill=marker_fill, outline=outline_color, width=1)
                            else:
                                draw_split_line(x1, y1, rating1, x2, y2, rating2, color)
                
                for (x, y, date, rating, point_type) in points:
                    if rating >= ELITE_THRESHOLD:
                        if point_type == 'end':
                            canvas.create_oval(x-4, y-4, x+4, y+4, fill='#1a1a1a', outline=color, width=2)
                        elif date == graph_data['start_point_date_str']:
                            canvas.create_oval(x-4, y-4, x+4, y+4, fill='#1a1a1a', outline=color, width=2)
                        else:
                            canvas.create_oval(x-3, y-3, x+3, y+3, fill=color, outline='white', width=1)
                
                team_points[team_name] = points
            
            rightmost_label_edge = end_x
            for team_name in graph_data['teams_by_rating']:
                if team_name in declined_teams:
                    continue
                points = team_points.get(team_name, [])
                if points:
                    final_x, final_y = points[-1][0], points[-1][1]
                    color = graph_data['team_color_map'][team_name]
                    display_rating = team_display_ratings.get(team_name, points[-1][3])
                    label_id = canvas.create_text(final_x + 15, final_y, text=f"{team_name}  {int(display_rating)}", font=('Arial', 9, 'bold'), fill=color, anchor='w')
                    bbox = canvas.bbox(label_id)
                    if bbox and bbox[2] > rightmost_label_edge:
                        rightmost_label_edge = bbox[2]
            
            min_date_spacing, dates_to_show = 100, []
            sorted_date_positions = sorted(date_to_x.items(), key=lambda x: x[1])
            if sorted_date_positions:
                last_x = sorted_date_positions[-1][1] + min_date_spacing
                for date, x in reversed(sorted_date_positions):
                    if last_x - x >= min_date_spacing:
                        dates_to_show.append((x, date))
                        last_x = x
                dates_to_show.reverse()
                if len(dates_to_show) < 2 and len(sorted_date_positions) >= 2:
                    dates_to_show = [sorted_date_positions[-2], sorted_date_positions[-1]]
            for x, date in dates_to_show:
                canvas.create_text(x, y_elite + 15, text='^', font=('Arial', 9), fill='#888888')
                canvas.create_text(x, y_elite + 35, text=date, font=('Arial', 8), fill='#888888', anchor='n')
            
            if end_date_str:
                canvas.create_text(end_x, y_elite + 15, text='v', font=('Arial', 10, 'bold'), fill='#FFFFFF')
                canvas.create_text(end_x, y_elite + 50, text=f"END: {end_date_str}", font=('Arial', 9, 'bold'), fill='#FFFFFF', anchor='n')
            
            legend_padding = 40
            gap_after_labels = 30
            available_space_start = rightmost_label_edge + gap_after_labels
            available_space_end = canvas_width - legend_padding
            center_point = (available_space_start + available_space_end) // 2
            estimated_legend_width = 350
            legend_x = center_point - (estimated_legend_width // 2)
            min_legend_x = rightmost_label_edge + gap_after_labels
            if legend_x < min_legend_x:
                legend_x = min_legend_x
            
            canvas.create_text(legend_x, margin_top, text="Teams:", font=('Arial', 11, 'bold'), fill='white', anchor='w')
            
            legend_data = []
            for team_name in sorted_teams:
                actual_final = graph_data['team_actual_finals'][team_name]
                initial_rating = graph_data['initial_ratings'].get(team_name, 1000)
                is_declined = actual_final < ELITE_THRESHOLD
                
                legend_data.append({
                    'name': team_name, 
                    'final': actual_final, 
                    'initial': initial_rating, 
                    'diff': actual_final - initial_rating, 
                    'declined': is_declined,
                    'color': graph_data['team_color_map'][team_name]
                })
            
            elite_split_idx = 0
            for i, data in enumerate(legend_data):
                if data['final'] < ELITE_THRESHOLD:
                    elite_split_idx = i
                    break
            else:
                elite_split_idx = len(legend_data)
            
            # Build global rank lookup from full rankings (excluding provisional)
            all_ranked = [t for t in get_sorted_rankings(include_archived=True) if not is_provisional(t)]
            global_rank_map = {t: i + 1 for i, t in enumerate(all_ranked)}

            rank_colors = {0: '#FFD700', 1: '#C0C0C0', 2: '#CD7F32'}
            for i, data in enumerate(legend_data):
                y = margin_top + 25 + (i * 35)
                if i == elite_split_idx and elite_split_idx > 0 and elite_split_idx < len(legend_data):
                    sep_y = y - 5
                    canvas.create_line(legend_x, sep_y, legend_x + 350, sep_y, fill='#444444', dash=(3, 3), width=1)
                    text_y = sep_y - 5
                    canvas.create_text(legend_x, text_y, text=f"v Below {ELITE_THRESHOLD} ELO v", font=('Arial', 7), fill='#666666', anchor='w')
                
                global_rank = global_rank_map.get(data['name'])
                rank_label = f"#{global_rank}" if global_rank else "—"
                canvas.create_rectangle(legend_x, y, legend_x + 14, y + 14, fill=data['color'], outline='#666666' if data['declined'] else 'white', width=1)
                canvas.create_text(legend_x + 20, y + 7, text=rank_label, font=('Arial', 8, 'bold'), fill='#666666', anchor='w')
                short_name = data['name'][:18] + "..." if len(data['name']) > 18 else data['name']
                name_color = '#888888' if data['declined'] else rank_colors.get(global_rank - 1 if global_rank else None, '#AAAAAA')
                canvas.create_text(legend_x + 45, y + 7, text=short_name, font=('Arial', 9, 'bold'), fill=name_color, anchor='w')
                sign = "+" if data['diff'] >= 0 else ""
                canvas.create_text(legend_x + 220, y + 7, text=f"{int(data['initial'])} -> {int(data['final'])} ({sign}{int(data['diff'])})", font=('Arial', 8), fill='#666666' if data['declined'] else '#888888', anchor='w')
            
            if close_btn[0] is None:
                close_btn[0] = tk.Button(root, text="Close", command=root.destroy, bg='#404040', fg='white', font=('Arial', 10), padx=30, pady=5)
            close_btn[0].place(x=canvas_width//2 - 40, y=canvas_height - 50)
            
        except Exception as e:
            logger.error(f"Draw error: {e}")
            import traceback
            traceback.print_exc()
    
    draw_graph(width, height)
    root.mainloop()

def _display_elite_teams_text(team_history, all_dates, elite_teams, final_ratings, initial_ratings, start_date, end_date):
    """
    Text-based fallback display for elite teams trend.
    """
    print("\n" + "="*80)
    print("  ELITE TEAMS OVER TIME")
    print("="*80)
    print(f"\n  Date Range: {start_date} to {end_date}")
    print(f"  Rating Range: {ELITE_THRESHOLD} - {RATING_CAP} CSRS Elo")
    print(f"  Elite Teams: {len(elite_teams)}")
    print(f"  Unique Dates: {len(all_dates)}\n")
    sorted_teams = sorted(elite_teams, key=lambda t: final_ratings.get(t, 0), reverse=True)
    for rank, team_name in enumerate(sorted_teams, 1):
        matches = team_history.get(team_name, [])
        if not matches: continue
        initial_rating = initial_ratings.get(team_name, 1000)
        final_rating = matches[-1][1] if matches else initial_rating
        diff = final_rating - initial_rating
        sign = "+" if diff >= 0 else ""
        status = "v Declined" if final_rating < ELITE_THRESHOLD else "[OK] Elite"
        print(f"  #{rank} {team_name} [{status}]")
        print(f"      Rating: {int(initial_rating)} -> {int(final_rating)} ({sign}{int(diff)})")
        print(f"      Matches: {len(matches)}\n")
    print("="*80)


# =============================================================================
# === TEAM MANAGEMENT HELPERS ===
# =============================================================================

def simulate_depreciation_menu() -> None:
    """Simulate rating depreciation for a team based on inactivity."""
    target_raw = check_cmd(input("Enter team name: "))
    if get_cmd(target_raw) in ['back', '0']:
        return
    
    target = find_team(target_raw)
    if not target:
        print(">>> Team not found.")
        return
    
    days_raw = check_cmd(input(f"Days since {target} played: "))
    if get_cmd(days_raw) in ['back', '0']:
        return
    
    try:
        days = int(days_raw)
        if days < 0:
            print("  [!] Days cannot be negative.")
            return
        
        depreciated = calculate_depreciation(teams[target], days, target)
        dep_loss = teams[target] - depreciated
        
        print(f"\n{'='*50}")
        print(f"  DEPRECIATION SIMULATION: {target}")
        print(f"{'='*50}")
        print(f"  Current Rating:     {int(teams[target]):>6}")
        print(f"  Days Inactive:      {days:>6}")
        print(f"{'-'*50}")
        
        if days > DEPRECIATION_THRESHOLD:
            form = calculate_form(target, n=15, history=history)
            form_score = form[1] if form else 50
            form_modifier = 1.0 - ((form_score - 50) / 250)
            form_modifier = max(FORM_MODIFIER_MIN, min(FORM_MODIFIER_MAX, form_modifier))
            
            print(f"  Form Score:       {form_score:>6} (if form exists)")
            print(f"  Form Modifier:    {form_modifier:>6.2f}x")
            print(f"{'-'*50}")
            print(f"  Depreciated:      {int(depreciated):>6}")
            print(f"  Rating Loss:      -{int(dep_loss):>5}")
            print(f"  Loss Percentage:  {(dep_loss/teams[target]*100):>5.1f}%")
        else:
            print(f"  [!] No depreciation (threshold is 7 days)")
            print(f"  Rating remains:   {int(teams[target]):>6}")
        
        print(f"{'='*50}")
    except ValueError:
        print("  [!] Invalid number.")

def create_team_menu():
    name = check_cmd(input("New team name: "))
    if get_cmd(name) == 'back': return
    print("Points Type: 1. CSRS Elo | 2. VRS Points")
    pt_choice = get_cmd(check_cmd(input("Select: ")))
    if pt_choice == 'back': return
    try:
        val_raw = check_cmd(input("Enter Value: "))
        if get_cmd(val_raw) == 'back': return
        val = float(val_raw)
        if pt_choice == '2': val = val / 2
        teams[name] = val
        mark_unsaved()
        print(f"Created {name} with {int(val)} CSRS Elo.")
    except ValueError:
        print("Invalid points entered.")

def delete_team_menu() -> None:
    """Delete a team from the roster."""
    target_raw = check_cmd(input("Delete team name: "))
    if get_cmd(target_raw) in ['back', '0']:
        return
    
    target = find_team(target_raw)
    if not target:
        print(">>> Team not found.")
        return
    
    # Count matches involving this team
    match_count = sum(1 for m in history if m.get('t1', {}).get('name') == target or m.get('t2', {}).get('name') == target)
    
    if match_count > 0:
        print(f"  [!] WARNING: {target} appears in {match_count} match(es).")
        print("      Deleting will not remove match history, but may cause display issues.")
    
    confirm = get_cmd(check_cmd(input(f"Confirm delete '{target}'? Type 'DELETE' to confirm: ")))
    if confirm != 'delete':
        print(">>> Cancelled.")
        return
    
    del teams[target]
    mark_unsaved()
    print(">>> Deleted.")

def rename_team_menu() -> None:
    """Rename an existing team."""
    target_raw = check_cmd(input("Old team name: "))
    if get_cmd(target_raw) in ['back', '0']:
        return
    
    target = find_team(target_raw)
    if not target:
        print(">>> Team not found.")
        return
    
    new_n = check_cmd(input("New team name: "))
    if get_cmd(new_n) in ['back', '0']:
        return
    
    if not new_n.strip():
        print("  [!] Team name cannot be empty.")
        return
    
    teams[new_n] = teams.pop(target)
    mark_unsaved()
    print(f">>> Renamed {target} to {new_n}.")

def manage_aliases_menu() -> None:
    """Manage team name aliases."""
    while True:
        print_menu(
            "MANAGE ALIASES",
            [
                ("1", "Add Alias"),
                ("2", "Remove Alias"),
                ("3", "View All"),
                (None, None),
                ("0", "Back"),
            ],
        )
        
        a_choice = get_cmd(check_cmd(input("Select: ")))
        if a_choice in ['0', 'back']:
            break
        elif a_choice == '1':
            alias_raw = check_cmd(input("Alias: ")).strip()
            if get_cmd(alias_raw) in ['back', '0']:
                continue
            if not alias_raw:
                print("  [!] Alias cannot be empty.")
                continue
            
            team_raw = check_cmd(input("Maps to team: ")).strip()
            if get_cmd(team_raw) in ['back', '0']:
                continue
            
            target = find_team(team_raw)
            if not target:
                print("  [!] Team not found.")
                continue
            
            aliases[alias_raw.lower()] = target
            mark_unsaved()
            save_aliases()
            print(f">>> Added: '{alias_raw}' -> '{target}'")
        elif a_choice == '2':
            alias_raw = check_cmd(input("Alias to remove: ")).strip()
            if get_cmd(alias_raw) in ['back', '0']:
                continue
            
            if alias_raw.lower() in aliases:
                del aliases[alias_raw.lower()]
                mark_unsaved()
                save_aliases()
                print(f">>> Removed alias '{alias_raw}'.")
            else:
                print("  [!] Alias not found.")
        elif a_choice == '3':
            if aliases:
                print("\nCurrent Aliases:")
                for alias, team in sorted(aliases.items()):
                    print(f"  '{alias}' -> '{team}'")
            else:
                print("  [!] No aliases defined.")

def get_archived_teams(today=None) -> List[Tuple[str, int, int]]:
    """
    Return [(team_name, depreciated_rating, days_inactive), ...] for every
    non-provisional team past INACTIVE_ARCHIVE_DAYS, sorted by days inactive
    (longest-gone first). Shared by manage_archived_teams_menu() and anything
    else that wants the archived list without re-deriving it.
    """
    if today is None:
        today = datetime.now().date()
    date_index = build_match_date_index(history)
    archived = []
    for name, points in teams.items():
        if is_provisional(name):
            continue
        last_match = get_team_last_match_date_before(name, index=date_index)
        if last_match is None:
            continue
        days_inactive = (today - last_match.date()).days
        if days_inactive > INACTIVE_ARCHIVE_DAYS:
            rating = int(calculate_depreciation(points, days_inactive, name)) if days_inactive > DEPRECIATION_THRESHOLD else int(points)
            archived.append((name, rating, days_inactive))
    archived.sort(key=lambda x: x[2], reverse=True)
    return archived


def manage_archived_teams_menu() -> None:
    """
    View and optionally bulk-delete teams that have been archived for
    long-term inactivity (INACTIVE_ARCHIVE_DAYS+, see is_team_archived).

    Archiving itself needs no action here — it's automatic and reversible
    (a team leaves this list the instant it plays a new match). This menu
    exists purely so the roster doesn't have to grow forever: if you want to
    actually prune teams that have been gone for a very long time, you can
    do it here without hunting them down one at a time in Delete Team.
    """
    archived = get_archived_teams()

    if not archived:
        print(f"\n>>> No archived teams. (Teams inactive {INACTIVE_ARCHIVE_DAYS}+ days show up here.)")
        return

    print(f"\n=== ARCHIVED / INACTIVE TEAMS ({len(archived)}) ===")
    print(f"  Inactive {INACTIVE_ARCHIVE_DAYS}+ days — hidden from rankings/graphs, but not deleted.")
    print(f"  They reappear automatically the moment they play a new match.\n")
    print(f"  {'#':<4} {'Team':<26} {'Rating':<10} {'Days Inactive'}")
    print(f"  {'-'*55}")
    for i, (name, rating, days_inactive) in enumerate(archived, 1):
        print(f"  {i:<4} {name:<26} {rating:<10} {days_inactive}")

    print_menu(
        "ARCHIVED TEAMS",
        [
            ("1", "Delete One (by number)"),
            ("2", "Delete All Archived Teams"),
            (None, None),
            ("0", "Back"),
        ],
    )
    choice = get_cmd(check_cmd(input("Select: ")))
    if choice in ['0', 'back']:
        return

    if choice == '1':
        try:
            idx_raw = check_cmd(input(f"Enter number to delete (1-{len(archived)}) or '0' to cancel: "))
            if get_cmd(idx_raw) in ['back', '0']:
                return
            idx = int(idx_raw) - 1
            if not (0 <= idx < len(archived)):
                print("  [!] Invalid number.")
                return
            target_name = archived[idx][0]
        except ValueError:
            print("  [!] Invalid input.")
            return

        confirm = get_cmd(check_cmd(input(f"Confirm delete '{target_name}'? Type 'DELETE' to confirm: ")))
        if confirm != 'delete':
            print(">>> Cancelled.")
            return
        del teams[target_name]
        mark_unsaved()
        print(f">>> Deleted '{target_name}'. Match history is untouched.")

    elif choice == '2':
        print(f"  [!] WARNING: This will permanently remove all {len(archived)} archived team(s) from the roster.")
        print("      Match history involving them is NOT removed and will remain in Match History.")
        confirm = get_cmd(check_cmd(input(f"Type 'DELETE ALL' to confirm: ")))
        if confirm != 'delete all':
            print(">>> Cancelled.")
            return
        for name, _, _ in archived:
            teams.pop(name, None)
        mark_unsaved()
        print(f">>> Deleted {len(archived)} archived team(s).")
    else:
        print("  [!] Invalid option.")


# =============================================================================
# === MENU: TEAM MANAGEMENT ===
# =============================================================================

def team_management_menu() -> None:
    """Menu for managing team roster only."""
    options = [
        ('1', 'Create Team', create_team_menu),
        ('2', 'Delete Team', delete_team_menu),
        ('3', 'Rename Team', rename_team_menu),
        ('4', 'Manage Aliases', manage_aliases_menu),
        ('5', 'Custom Point Adjustments', custom_point_adjustments),
        ('6', 'Simulate Depreciation', simulate_depreciation_menu),
        ('7', 'Archived / Inactive Teams', manage_archived_teams_menu),
        ('0', 'Back', None),
    ]
    
    while True:
        print_menu(
            "TEAM MANAGEMENT",
            [
                ("1", "Create Team"),
                ("2", "Delete Team"),
                ("3", "Rename Team"),
                ("4", "Manage Aliases"),
                (None, None),
                ("5", "Custom Point Adjustments"),
                ("6", "Simulate Depreciation"),
                ("7", "Archived / Inactive Teams"),
                (None, None),
                ("0", "Back"),
            ],
            subtitle="Roster Operations",
        )
        
        choice = get_cmd(check_cmd(input("Select: ")))
        
        if choice in ['0', 'back']:
            break
        
        found = False
        for num, _, func in options:
            if choice == num:
                func()
                found = True
                break
        if not found:
            print_warning("Invalid choice. Try again.")

# =============================================================================
# === HISTORY MANAGEMENT (RESIMULATION & DELETION) ===
# =============================================================================

def _create_backup():
    """Create a rotated backup of data.save (backup_1.save most recent)."""
    _backup_dir = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "save", "backup")
    os.makedirs(_backup_dir, exist_ok=True)
    try:
        for i in range(2, 0, -1):
            src = os.path.join(_backup_dir, f"backup_{i}.save")
            dst = os.path.join(_backup_dir, f"backup_{i+1}.save")
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(SAVE_FILE):
            import shutil
            shutil.copy2(SAVE_FILE, os.path.join(_backup_dir, "backup_1.save"))
    except Exception:
        pass

def _do_resimulation(history_list: List[Dict[str, Any]], verbose: bool = True) -> None:
    """
    Internal: fully resimulate ratings from the given history list.
    DEPRECIATION IS APPLIED BETWEEN MATCHES BASED ON TIME GAPS.
    PROVISIONAL-TEAM HANDLING IS APPLIED IDENTICALLY TO THE IMPORT-TIME PATH
    (see _import_url_list): capped rating differential vs provisional
    opponents, boosted K-factor for a provisional team's own change, and
    graduation after PROVISIONAL_MATCH_THRESHOLD matches.

    NOTE on provisional inference: provisional registration is normally
    decided at scrape time by a live VRS lookup (see scrape_vrs_points /
    "register as provisional" elsewhere in this file), which isn't
    available during a pure history replay. We instead infer it from data
    already in the history record: a team is treated as having started
    provisional if its pts_before on its very first chronological match
    equals exactly PROVISIONAL_STARTING_RATING (400) — the fixed value
    only ever assigned when no VRS rating was found at original import
    time. From there, match counts and graduation are reconstructed
    chronologically using the same PROVISIONAL_MATCH_THRESHOLD /
    PROVISIONAL_K_FACTORS as the live importer, so a team's provisional
    window during resimulation lines up with what actually happened at
    import time
    """
    global teams, peak_ratings, provisional_teams
    
    if not history_list:
        if verbose:
            print("\n>>> No match history to resimulate.")
        return

    pre_resim_ratings: Dict[str, float] = dict(teams)

    if verbose:
        print(f"\n>>> Resimulating {len(history_list)} matches from scratch...")
        print(f">>> Depreciation will be applied between matches (7+ days inactivity)")
        if len(history_list) > 0:
            first = history_list[0]
            last = history_list[-1]
            print(f">>> First match: {first.get('date')} | {first.get('t1', {}).get('name')} vs {first.get('t2', {}).get('name')}")
            print(f">>> Last match: {last.get('date')} | {last.get('t1', {}).get('name')} vs {last.get('t2', {}).get('name')}")

    sim_teams: Dict[str, float] = dict(STARTING_TEAMS)

    # Local, isolated provisional-status tracker for this resimulation run —
    # mirrors the global provisional_teams dict's shape (team_name -> matches
    # played while provisional) but never touches the real global mid-loop,
    # exactly like sim_teams vs. the real teams dict above. Only written
    # back to the real global once, at the very end of resimulation.
    sim_provisional: Dict[str, int] = {}

    chronologically_sorted: List[Dict[str, Any]] = sorted(history_list, key=lambda m: (m.get('date') == 'N/A', m.get('date', '')))

    # Pre-parse every match date once up front instead of re-parsing the
    # same string with datetime.strptime() inside the hot loop below —
    # strptime is comparatively slow and this ran once per match every
    # single resimulation.
    parsed_dates: List[datetime] = []
    for m in chronologically_sorted:
        raw = m.get('date', 'N/A')
        try:
            parsed_dates.append(datetime.strptime(raw[:10], "%Y-%m-%d"))
        except Exception:
            parsed_dates.append(datetime.now())

    # Track last match date per team for depreciation calculation
    team_last_match: Dict[str, datetime] = {}

    # Per-team sliding window of (side, match) tuples for incremental form
    # tracking. Replaces the old approach of calling calculate_form() with
    # a freshly-sliced history_list[:match_idx] on every single match
    # (O(N) list copy + O(N) scan, done twice per match => O(N^2) overall).
    # Since matches are processed strictly in chronological order here, we
    # can just append each match to the relevant teams' deques right after
    # it's processed and use the current deque contents (already capped at
    # the last 15) to compute form directly — O(1) maintenance, O(15) form
    # calc, so O(N) overall.
    team_recent_matches: Dict[str, deque] = {}
    
    teams_first_pts_before: Dict[str, float] = {}
    for m in chronologically_sorted:
        for side in ['t1', 't2']:
            team_data = m.get(side, {})
            team_name = team_data.get('name')
            pts_before = team_data.get('pts_before')
            
            if team_name and team_name not in STARTING_TEAMS:
                if team_name not in teams_first_pts_before and pts_before is not None:
                    teams_first_pts_before[team_name] = pts_before
    
    for team_name, start_pts in teams_first_pts_before.items():
        sim_teams[team_name] = start_pts
        if verbose:
            old_rating = pre_resim_ratings.get(team_name, 0)
            change = start_pts - old_rating
            change_str = f"{'+' if change >= 0 else ''}{int(change)}"
            print(f"  [OK] {team_name}: Starting at {int(start_pts)} (change: {change_str})")
        # Infer provisional start: only ever assigned PROVISIONAL_STARTING_RATING
        # exactly when no VRS rating was found at original import time.
        if start_pts == PROVISIONAL_STARTING_RATING:
            sim_provisional[team_name] = 0
    
    for name, pts in teams.items():
        if name not in sim_teams:
            sim_teams[name] = pts
            if verbose:
                print(f"  [!] {name}: Starting at {int(pts)} (current rating - NO HISTORY DATA)")
    
    if verbose:
        print(f"\n>>> Total teams in resimulation: {len(sim_teams)}")
        if teams_first_pts_before:
            print(f">>> Teams with starting ratings from history: {len(teams_first_pts_before)}")
        if sim_provisional:
            print(f">>> Teams inferred provisional at history start: {len(sim_provisional)}")

    if verbose:
        print(f"\n>>> Processing matches...")

    resim_start = time.monotonic()
    resim_durations: list = []  # per-match times for rolling average

    peak_ratings.clear()

    # Incremental rank tracker: rank_shift is purely cosmetic (stored in
    # the history record only, never fed back into rating math), so
    # instead of doing two full O(K log K) sorts of every team on every
    # single match, maintain one running sorted list of all current
    # ratings and use O(log K) bisect lookups to find a team's rank
    # (count of teams with a strictly higher rating, + 1), with O(K)
    # insert/remove on rating changes (still cheaper in practice than
    # re-sorting all K teams from scratch twice per match).
    sim_points_sorted: List[float] = sorted(sim_teams.values())

    def _rank_of(value: float) -> int:
        greater = len(sim_points_sorted) - bisect.bisect_right(sim_points_sorted, value)
        return greater + 1

    def _resort_update(old_value: float, new_value: float) -> None:
        idx = bisect.bisect_left(sim_points_sorted, old_value)
        # idx should point at an occurrence of old_value; guard just in
        # case of float drift so we never desync the tracker.
        if idx < len(sim_points_sorted) and sim_points_sorted[idx] == old_value:
            del sim_points_sorted[idx]
        else:
            sim_points_sorted.remove(old_value)
        bisect.insort(sim_points_sorted, new_value)
    
    for match_idx, m in enumerate(chronologically_sorted, 1):
        _match_t0 = time.monotonic()
        t1_data = m.get('t1') or {}
        t2_data = m.get('t2') or {}
        t1_name = t1_data.get('name')
        t2_name = t2_data.get('name')
        t1_score = t1_data.get('score', 0)
        t2_score = t2_data.get('score', 0)
        tier = m.get('tier', 'A')
        env = m.get('env', 'LAN')
        is_gf = m.get('grand_final', False)
        match_date_str = m.get('date', 'N/A')
        
        if not t1_name or not t2_name:
            if verbose:
                print_warning(f"Skipping match {match_idx}: Missing team names")
            continue
        
        match_date = parsed_dates[match_idx - 1]
        
        # Handle orphaned teams (in history but not in roster)
        if t1_name not in sim_teams:
            logger.warning(f"Team '{t1_name}' in history but not in roster. Assigning 1000 pts.")
            sim_teams[t1_name] = 1000
            bisect.insort(sim_points_sorted, 1000)
        if t2_name not in sim_teams:
            logger.warning(f"Team '{t2_name}' in history but not in roster. Assigning 1000 pts.")
            sim_teams[t2_name] = 1000
            bisect.insort(sim_points_sorted, 1000)
        
        # === FORM (calculated from a window that INCLUDES this match's own
        # already-known score) ===
        # NOTE: the original implementation computed this via
        # calculate_form(team, n=15, history=history_list[:match_idx]).
        # Since match_idx is 1-based, that slice's exclusive upper bound
        # lands one past the current match's own 0-based index — i.e. it
        # includes the current match itself (its score fields are already
        # present in the record even though pts_before/pts_after for this
        # match haven't been computed yet). To reproduce that exactly, the
        # current match is pushed onto each team's window before form is
        # computed, not after.
        if t1_name not in team_recent_matches:
            team_recent_matches[t1_name] = deque(maxlen=15)
        team_recent_matches[t1_name].append(('t1', m))
        if t2_name not in team_recent_matches:
            team_recent_matches[t2_name] = deque(maxlen=15)
        team_recent_matches[t2_name].append(('t2', m))

        form1 = _compute_form_from_recent(team_recent_matches[t1_name])
        form2 = _compute_form_from_recent(team_recent_matches[t2_name])
        form_adj_1 = (form1[1] - 50) if form1 else 0
        form_adj_2 = (form2[1] - 50) if form2 else 0
        form_score_1 = form1[1] if form1 else None
        form_score_2 = form2[1] if form2 else None

        # === APPLY DEPRECIATION BASED ON TIME SINCE LAST MATCH ===
        p1_before = sim_teams[t1_name]
        p2_before = sim_teams[t2_name]

        # Team 1 depreciation
        if t1_name in team_last_match:
            days_inactive_1 = (match_date - team_last_match[t1_name]).days
            if days_inactive_1 > DEPRECIATION_THRESHOLD:
                p1_before = calculate_depreciation(p1_before, days_inactive_1, t1_name, form_score=form_score_1)
                if verbose and match_idx % 20 == 0:
                    print(f"  [Dep] {t1_name}: -{int(sim_teams[t1_name] - p1_before)} pts ({days_inactive_1}d inactive)")

        # Team 2 depreciation
        if t2_name in team_last_match:
            days_inactive_2 = (match_date - team_last_match[t2_name]).days
            if days_inactive_2 > DEPRECIATION_THRESHOLD:
                p2_before = calculate_depreciation(p2_before, days_inactive_2, t2_name, form_score=form_score_2)
                if verbose and match_idx % 20 == 0:
                    print(f"  [Dep] {t2_name}: -{int(sim_teams[t2_name] - p2_before)} pts ({days_inactive_2}d inactive)")
        
        t1_won = 1 if t1_score > t2_score else 0
        t2_won = 1 if t2_score > t1_score else 0
        map_diff = abs(t1_score - t2_score)

        # === PROVISIONAL STATUS (inferred chronologically, see docstring) ===
        t1_prov = t1_name in sim_provisional
        t2_prov = t2_name in sim_provisional
        t1_k = PROVISIONAL_K_FACTORS.get(sim_provisional.get(t1_name, 0) + 1, 1.0) if t1_prov else 1.0
        t2_k = PROVISIONAL_K_FACTORS.get(sim_provisional.get(t2_name, 0) + 1, 1.0) if t2_prov else 1.0

        forfeit = m.get('forfeit')  # 'team1', 'team2', or None
        if forfeit == 'team1':
            raw_p1 = calculate_points(p1_before, p2_before, 0, map_diff or 1, tier, env, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
            raw_p2 = p2_before
        elif forfeit == 'team2':
            raw_p1 = p1_before
            raw_p2 = calculate_points(p2_before, p1_before, 0, map_diff or 1, tier, env, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)
        else:
            raw_p1 = calculate_points(p1_before, p2_before, t1_won, map_diff, tier, env, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
            raw_p2 = calculate_points(p2_before, p1_before, t2_won, map_diff, tier, env, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)

        # Apply provisional K-multiplier to the team's own rating change
        # (not the opponent's), clamped to RATING_FLOOR/RATING_CAP — same
        # math as the live importer.
        if t1_prov:
            new_p1 = min(max(RATING_FLOOR, p1_before + (raw_p1 - p1_before) * t1_k), RATING_CAP)
        else:
            new_p1 = raw_p1
        if t2_prov:
            new_p2 = min(max(RATING_FLOOR, p2_before + (raw_p2 - p2_before) * t2_k), RATING_CAP)
        else:
            new_p2 = raw_p2

        # Increment / graduate provisional status — same rule as
        # increment_provisional(): the forfeiting team doesn't get credit
        # for a match it forfeited.
        if t1_prov and (not forfeit or forfeit == 'team1'):
            sim_provisional[t1_name] = sim_provisional.get(t1_name, 0) + 1
            if sim_provisional[t1_name] >= PROVISIONAL_MATCH_THRESHOLD:
                del sim_provisional[t1_name]
        if t2_prov and (not forfeit or forfeit == 'team2'):
            sim_provisional[t2_name] = sim_provisional.get(t2_name, 0) + 1
            if sim_provisional[t2_name] >= PROVISIONAL_MATCH_THRESHOLD:
                del sim_provisional[t2_name]
        
        t1_old_rank = _rank_of(sim_teams[t1_name])
        t2_old_rank = _rank_of(sim_teams[t2_name])

        _resort_update(sim_teams[t1_name], new_p1)
        _resort_update(sim_teams[t2_name], new_p2)

        sim_teams[t1_name] = new_p1
        sim_teams[t2_name] = new_p2

        t1_new_rank = _rank_of(new_p1)
        t2_new_rank = _rank_of(new_p2)
        
        t1_rank_shift = t1_old_rank - t1_new_rank
        t2_rank_shift = t2_old_rank - t2_new_rank
        
        # Store match data WITH DEPRECIATED pts_before
        m['t1']['pts_before'] = p1_before  # This is now depreciated rating
        m['t1']['pts_after'] = new_p1
        m['t1']['rank_shift'] = t1_rank_shift
        m['t2']['pts_before'] = p2_before  # This is now depreciated rating
        m['t2']['pts_after'] = new_p2
        m['t2']['rank_shift'] = t2_rank_shift
        
        update_peak(t1_name, new_p1, match_date_str)
        update_peak(t2_name, new_p2, match_date_str)
        
        # Update last match date for depreciation tracking
        team_last_match[t1_name] = match_date
        team_last_match[t2_name] = match_date
        
        resim_durations.append(time.monotonic() - _match_t0)

        pct = match_idx * 100 // len(chronologically_sorted)
        prev_pct = (match_idx - 1) * 100 // len(chronologically_sorted)
        if pct != prev_pct or match_idx == len(chronologically_sorted):
            total_elapsed = time.monotonic() - resim_start
            window = resim_durations[-50:]  # rolling average over last 50
            avg = sum(window) / len(window)
            remaining = max(0, len(chronologically_sorted) - match_idx)
            eta_sec = avg * remaining
            finish_dt = datetime.now().astimezone() + timedelta(seconds=eta_sec)
            elapsed_str = _format_duration(total_elapsed)
            eta_str = _format_duration(eta_sec)
            print(
                f'\r  Resimulating: {pct:3d}% ({match_idx}/{len(chronologically_sorted)}) | '
                f'elapsed: {elapsed_str} | '
                f'remaining: {eta_str} | '
                f'ETA: {finish_dt.strftime("%H:%M:%S")}',
                end='', flush=True
            )
            if match_idx == len(chronologically_sorted):
                print()

    teams.clear()
    teams.update(sim_teams)

    # Write back inferred provisional state to the real global, same as
    # teams above — only done once, after the full chronological replay,
    # so any team still mid-provisional-window at the end of history
    # correctly stays provisional going forward.
    provisional_teams.clear()
    provisional_teams.update(sim_provisional)
    if verbose and sim_provisional:
        print(f">>> {len(sim_provisional)} team(s) still provisional at end of resimulation: "
              f"{', '.join(sorted(sim_provisional.keys()))}")
    
    if verbose:
        print(f"\n>>> Resimulation complete! {len(chronologically_sorted)} matches processed.")
        print(f">>> Depreciation applied between matches where inactivity > 7 days")
        print(f"\n=== FINAL RATING CHANGES ===")
        
        changes = []
        for team_name in sim_teams:
            old_rating = pre_resim_ratings.get(team_name, 0)
            new_rating = sim_teams[team_name]
            change = new_rating - old_rating
            changes.append((team_name, old_rating, new_rating, change))
        
        changes.sort(key=lambda x: abs(x[3]), reverse=True)
        
        print(f"  {'Team':<25} {'Before':<10} {'After':<10} {'Change':<10}")
        print(f"  {'-'*55}")
        for team_name, old_rating, new_rating, change in changes[:15]:
            change_str = f"{'+' if change >= 0 else ''}{int(change)}"
            print(f"  {team_name:<25} {int(old_rating):<10} {int(new_rating):<10} {change_str:<10}")
        
        print(f"\n>>> Team ratings updated.")

def resimulate():
    """
    Resimulate all matches from current global history list.
    """
    global history
    if not history:
        print("\n>>> No match history to resimulate.")
        return
    
    mark_unsaved()
    print(f">>> Current history contains {len(history)} matches")
    confirm = get_cmd(check_cmd(input(f"Resimulate {len(history)} matches with current formula (including form adjustments)? This will overwrite current team points and history. (y/n): ")))
    if confirm != 'y':
        print(">>> Cancelled.")
        return
    
    _do_resimulation(history, verbose=True)
    history = load_history()

def run_resimulate_command(skip_confirm: bool = False) -> None:
    """
    CLI entry point for `python CSRS.py --resimulate`.
    Loads all data, resimulates the full match history from scratch using
    the current formula/config, and saves. No menu involved.
    """
    global history
    load_all()

    if not history:
        print("\n>>> No match history to resimulate.")
        return

    print(f">>> Current history contains {len(history)} matches")
    if not skip_confirm:
        confirm = input(
            f"Resimulate {len(history)} matches with current formula "
            f"(including form adjustments)? This will overwrite current "
            f"team points and history. (y/n): "
        ).strip().lower()
        if confirm != 'y':
            print(">>> Cancelled.")
            return

    mark_unsaved()
    _do_resimulation(history, verbose=True)
    history = load_history()
    print(">>> Resimulation complete.")

def delete_and_resimulate(matches_to_delete):
    """
    Delete given matches from history and fully resimulate ratings.
    """
    global history
    history = load_history()
    if not matches_to_delete:
        print(">>> No matches to delete.")
        return

    matches_to_remove = [m for m in matches_to_delete if m in history]
    if not matches_to_remove:
        print(">>> No matching matches found to delete.")
        return

    _create_backup()

    n_before = len(history)
    for m in matches_to_remove:
        history.remove(m)
        mark_unsaved()
    n_deleted = n_before - len(history)

    if len(history) == 0:
        global teams, peak_ratings
        teams = dict(STARTING_TEAMS)
        peak_ratings = {}
        save_history(history)
        print(f">>> Deleted all {n_deleted} match(es). Team ratings reset to starting values.")
        return

    print(f"\n>>> Deleted {n_deleted} match(es). Resimulating ratings from scratch...")
    _do_resimulation(history, verbose=True)

def clear_history():
    """
    Clear all match history and reset team ratings to starting values.
    """
    global teams, peak_ratings
    _create_backup()
    mark_unsaved()
    save_history([])
    teams = dict(STARTING_TEAMS)
    peak_ratings = {}
    save_all()
    print(">>> History cleared and team ratings reset to starting values.")


def clear_history_menu() -> None:
    """Clear all match history with confirmation."""
    if not history:
        print("  [!] No history to clear.")
        return
    
    print(f"\n  [!] WARNING: This will delete {len(history)} match(es).")
    print("      Team ratings will be reset to starting values.")
    print("      This action CANNOT be undone (but backups exist).")
    
    confirm = get_cmd(check_cmd(input("Type 'CLEAR' to confirm: ")))
    if confirm != 'clear':
        print(">>> Cancelled.")
        return
    
    clear_history()
    load_history()
    print(">>> History cleared.")


# =============================================================================
# === MENU: MATCH HISTORY VIEWER ===
# =============================================================================

def view_all_matches() -> None:
    """Display all matches in history."""
    if not history:
        print("\n>>> No match history found.")
        return
    
    print(f"\n=== ALL MATCHES ({len(history)} total) ===")
    for i, m in enumerate(history, 1):
        _print_match_entry(i, m)
    print("\nPress Enter to return...")
    input()


def view_match_history() -> None:
    """Interactive menu to view, filter, edit, and delete match history."""
    global history
    history = load_history()
    
    if not history:
        print("\n>>> No match history found.")
        print("    Import matches from HLTV (Menu Option 1) to get started.")
        return

    history = sorted(history, key=lambda m: (m.get('date') == 'N/A', m.get('date', '')))

    options = [
        ('1', 'View All Matches', view_all_matches),
        ('2', 'Filter by Team', filter_history_by_team),
        ('3', 'Filter by Tier/Environment', filter_history_by),
        ('4', 'Filter by Date Range', history_within_date_range),
        ('5', 'Filter by Event', filter_history_by_event),
        ('6', 'Edit Match Details', edit_match_details),
        ('7', 'Delete Match', delete_match_menu),
        ('8', 'Resimulate All', resimulate),
        ('0', 'Back', None),
    ]
    
    while True:
        print_menu(
            "MATCH HISTORY",
            [
                ("1", "View All Matches"),
                ("2", "Filter by Team"),
                ("3", "Filter by Tier / Environment"),
                ("4", "Filter by Date Range"),
                ("5", "Filter by Event"),
                (None, None),
                ("6", "Edit Match Details"),
                ("7", "Delete Match"),
                (None, None),
                ("8", "Resimulate All"),
                (None, None),
                ("0", "Back"),
            ],
            subtitle=f"{len(history)} matches",
        )
        
        raw_choice = check_cmd(input("Select: ")).strip()
        choice = get_cmd(raw_choice)
        
        if choice in ['0', 'back']:
            break
        
        found = False
        for num, _, func in options:
            if choice == num:
                func()
                found = True
                break
        if not found:
            print_warning("Invalid choice. Try again.")


def get_match_event(m: dict) -> str:
    """Return event name from a match record, checking both 'event' and legacy 'event_name'."""
    return m.get('event') or m.get('event_name') or ''


def filter_history_by_event() -> None:
    """Filter match history by event name."""
    h = load_history()
    if not h:
        print("\n>>> No match history.")
        return

    # Sort by most recent match date, then alphabetically for events with no matches
    def event_recency(ev):
        dates = [m.get('date', '') for m in h if m.get('event') == ev and m.get('date')]
        return max(dates) if dates else ''
    events = sorted(set(m.get('event', 'Unknown') for m in h if m.get('event')), key=event_recency, reverse=True)
    if not events:
        print("\n>>> No event data found in match history.")
        return

    print("\n  --- Events in History ---")
    for i, ev in enumerate(events, 1):
        count = sum(1 for m in h if m.get('event') == ev)
        tier = event_tiers.get(ev, '?')
        print(f"  [{i:>2}] {ev:<40} Tier: {tier:<4} ({count} matches)")

    print()
    raw = check_cmd(input("  Enter event number or partial name (or 0 to back): ")).strip()
    if get_cmd(raw) in ['0', 'back']:
        return

    # Resolve selection — number or text search
    selected_event = None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(events):
            selected_event = events[idx]
        else:
            print_warning("Invalid selection.")
            return
    else:
        matches_found = [ev for ev in events if raw.lower() in ev.lower()]
        if len(matches_found) == 1:
            selected_event = matches_found[0]
        elif len(matches_found) > 1:
            print(f"\n  Multiple matches found:")
            for i, ev in enumerate(matches_found, 1):
                print(f"  [{i}] {ev}")
            sub = check_cmd(input("  Select number: ")).strip()
            if sub.isdigit() and 1 <= int(sub) <= len(matches_found):
                selected_event = matches_found[int(sub) - 1]
            else:
                return
        else:
            print_warning(f"No event matching '{raw}' found.")
            return

    filtered = [m for m in h if m.get('event') == selected_event]
    tier = event_tiers.get(selected_event, 'Unknown')
    print(f"\n  Matches for: {selected_event}  (Tier: {tier}, {len(filtered)} matches)\n")
    for i, m in enumerate(filtered):
        _print_match_entry(i, m)


def filter_history_by() -> None:
    """Filter match history by Tier and/or Environment."""
    history = load_history()
    if not history:
        print("\n>>> No match history.")
        return
    
    # Filter by Tier
    print_menu(
        "FILTER BY TIER",
        [
            ("1", "S+ Tier Only"),
            ("2", "S Tier Only"),
            ("3", "A Tier Only"),
            ("4", "B Tier Only"),
            ("5", "C Tier Only"),
            ("6", "D Tier Only"),
            ("7", "S+ and S Tier  (Major Events)"),
            ("8", "All Tiers"),
            (None, None),
            ("0", "Back"),
        ],
    )
    
    tier_choice = check_cmd(input("  Select: ")).strip()
    if get_cmd(tier_choice) in ['0', 'back']:
        return
    
    tier_filter = None
    tier_label = "All"
    
    if tier_choice == '1': tier_filter = ['S+']; tier_label = "S+"
    elif tier_choice == '2': tier_filter = ['S']; tier_label = "S"
    elif tier_choice == '3': tier_filter = ['A']; tier_label = "A"
    elif tier_choice == '4': tier_filter = ['B']; tier_label = "B"
    elif tier_choice == '5': tier_filter = ['C']; tier_label = "C"
    elif tier_choice == '6': tier_filter = ['D']; tier_label = "D"
    elif tier_choice == '7': tier_filter = ['S+', 'S']; tier_label = "S+ and S"
    elif tier_choice == '8': tier_filter = None; tier_label = "All"
    else:
        print("  [!] Invalid option.")
        return
    
    # Filter by Environment
    print_menu(
        "FILTER BY ENVIRONMENT",
        [
            ("1", "Online Only"),
            ("2", "LAN Only"),
            ("3", "All Environments"),
            (None, None),
            ("0", "Back"),
        ],
    )
    
    env_choice = check_cmd(input("  Select: ")).strip()
    if get_cmd(env_choice) in ['0', 'back']:
        return
    
    env_filter = None
    env_label = "All"
    
    if env_choice == '1': env_filter = ['ONLINE']; env_label = "Online"
    elif env_choice == '2': env_filter = ['LAN', 'STUDIO', 'STAGE']; env_label = "LAN"
    elif env_choice == '3': env_filter = None; env_label = "All"
    else:
        print("  [!] Invalid option.")
        return
    
    filtered = []
    for m in history:
        match_tier = m.get('tier', 'A')
        match_env = m.get('env', 'LAN')
        
        tier_match = tier_filter is None or match_tier in tier_filter
        env_match = env_filter is None or match_env in env_filter
        
        if tier_match and env_match:
            filtered.append(m)
    
    if not filtered:
        print(f"\n>>> No matches found with Tier={tier_label}, Environment={env_label}")
        return
    
    print(f"\n=== FILTERED MATCHES ({len(filtered)} found) ===")
    print(f"  Tier: {tier_label} | Environment: {env_label}")
    print(f"  {'-'*60}")
    
    for i, m in enumerate(filtered, 1):
        _print_match_entry(i, m)
    
    print(f"\n=== END OF FILTERED RESULTS ===")
    print("\nPress Enter to return...")
    input()


def filter_history_by_team() -> None:
    """Filter match history by a specific team."""
    team_raw = check_cmd(input("Enter team name: "))
    if get_cmd(team_raw) in ['back', '0']:
        return
    
    team = find_team(team_raw)
    if not team:
        print(">>> Team not found.")
        return
    
    filtered = [m for m in history if m.get('t1', {}).get('name', '').lower() == team.lower() or m.get('t2', {}).get('name', '').lower() == team.lower()]
    
    if not filtered:
        print(">>> No matches found for that team.")
    else:
        print(f"\n--- Matches for {team} ({len(filtered)} found) ---")
        for i, m in enumerate(filtered, 1):
            _print_match_entry(i, m)
        print("\nPress Enter to return...")
        input()


def history_within_date_range() -> None:
    """Filter match history by date range."""
    history = load_history()
    if not history:
        print("\n>>> No match history.")
        return

    print("\nEnter start date (YYYY-MM-DD) or '0' to cancel:")
    start_raw = input("> ").strip()
    if get_cmd(start_raw) in ['back', '0']:
        return
    try:
        start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
    except ValueError:
        print("  [!] Invalid date format. Use YYYY-MM-DD.")
        return

    print("Enter end date (YYYY-MM-DD) or press Enter for today:")
    end_raw = input("> ").strip()
    if get_cmd(end_raw) in ['back', '0']:
        return
    if not end_raw:
        end_date = datetime.now().date()
    else:
        try:
            end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
        except ValueError:
            print("  [!] Invalid date format.")
            return

    if start_date > end_date:
        print("  [!] Start date must be before or equal to end date.")
        return

    filtered = []
    for m in history:
        date_str = m.get('date')
        if not date_str or date_str == 'N/A':
            continue
        try:
            m_date_str = date_str.split()[0]
            m_date = datetime.strptime(m_date_str, "%Y-%m-%d").date()
            if start_date <= m_date <= end_date:
                filtered.append(m)
        except Exception:
            continue

    if not filtered:
        print("\n>>> No matches found in that date range.")
        return

    print(f"\n=== FILTERED MATCHES ({len(filtered)}) ===")
    for i, m in enumerate(filtered, 1):
        _print_match_entry(i, m)

    print("\nPress Enter to return...")
    input()


def delete_match_menu() -> None:
    """Delete specific matches from history."""
    history = load_history()
    if not history:
        print("\n>>> No match history to delete.")
        return
    
    print_menu(
        "DELETE MATCHES",
        [
            ("1", "Delete by Index"),
            ("2", "Delete by Team"),
            ("3", "Delete by Date"),
            ("4", "Delete Duplicates"),
            (None, None),
            ("0", "Back"),
        ],
        subtitle=f"{len(history)} matches total",
    )
    
    choice = get_cmd(check_cmd(input("Select: ")))
    if choice in ['0', 'back']:
        return
    
    if choice == '1':
        try:
            idx_raw = check_cmd(input(f"Enter match number to delete (1-{len(history)}) or '0' to cancel: "))
            if get_cmd(idx_raw) in ['back', '0']:
                return
            idx = int(idx_raw) - 1
            if 0 <= idx < len(history):
                confirm = get_cmd(check_cmd(input(f"Delete match #{idx+1}? (y/n): ")))
                if confirm == 'y':
                    delete_and_resimulate([history[idx]])
                    print(">>> Match deleted.")
            else:
                print("  [!] Invalid number.")
        except ValueError:
            print("  [!] Invalid input.")
    elif choice == '2':
        team_raw = check_cmd(input("Enter team name: "))
        if get_cmd(team_raw) in ['back', '0']:
            return
        team = find_team(team_raw)
        if team:
            matches_to_delete = [m for m in history if m.get('t1', {}).get('name') == team or m.get('t2', {}).get('name') == team]
            if matches_to_delete:
                confirm = get_cmd(check_cmd(input(f"Delete {len(matches_to_delete)} matches involving {team}? (y/n): ")))
                if confirm == 'y':
                    delete_and_resimulate(matches_to_delete)
                    print(">>> Matches deleted.")
            else:
                print("  [!] No matches found for this team.")
    elif choice == '3':
        date_raw = check_cmd(input("Enter date (YYYY-MM-DD): "))
        if get_cmd(date_raw) in ['back', '0']:
            return
        matches_to_delete = [m for m in history if m.get('date', '').startswith(date_raw)]
        if matches_to_delete:
            confirm = get_cmd(check_cmd(input(f"Delete {len(matches_to_delete)} matches from {date_raw}? (y/n): ")))
            if confirm == 'y':
                delete_and_resimulate(matches_to_delete)
                print(">>> Matches deleted.")
        else:
            print("  [!] No matches found for this date.")
    elif choice == '4':
        duplicate_match_detection()

def edit_match_details():
    """
    Allow editing environment and event name of a match.
    """
    global history
    if not history:
        print("\n>>> No match history to edit.")
        return False

    print_menu(
        "EDIT MATCH DETAILS",
        [
            ("1", "Show All Matches"),
            ("2", "Filter by Date"),
            (None, None),
            ("0", "Back"),
        ],
        subtitle=f"{len(history)} matches",
    )
    sub_choice = get_cmd(check_cmd(input("Select: ")))
    if sub_choice == '0' or sub_choice == 'back':
        return False

    matches_to_show = history
    if sub_choice == '2':
        available_dates = sorted(set(m.get('date', 'N/A') for m in history if m.get('date') != 'N/A'))
        if not available_dates:
            print(">>> No dated matches available.")
            return False
        print("\nAvailable dates:")
        for i, d in enumerate(available_dates, 1):
            print(f"  {i}. {d}")
        try:
            date_idx = int(check_cmd(input(f"\nSelect date number (1-{len(available_dates)}) or '0' to go back: ")).strip()) - 1
            if date_idx < 0 or date_idx >= len(available_dates):
                print(">>> Invalid selection.")
                return False
            filter_date = available_dates[date_idx]
            matches_to_show = [m for m in history if m.get('date') == filter_date]
        except ValueError:
            print(">>> Invalid input.")
            return False

    print(f"\nMatches:")
    for i, m in enumerate(matches_to_show, 1):
        t1_name = m.get('t1', {}).get('name', 'Unknown')
        t2_name = m.get('t2', {}).get('name', 'Unknown')
        s1 = m.get('t1', {}).get('score', 0)
        s2 = m.get('t2', {}).get('score', 0)
        date = m.get('date', 'N/A')
        env = m.get('env', 'N/A')
        event = m.get('event', 'N/A')
        tier = m.get('tier', 'N/A')
        gf_tag = f" [{m.get('match_stage')}]" if m.get('match_stage') else (" [GRAND FINAL]" if m.get('grand_final') else "")
        print(f"  {i}. {date}{gf_tag}: {t1_name} {s1} - {s2} {t2_name} | Env: {env} | Tier: {tier} | Event: {event}")

    try:
        idx_raw = check_cmd(input(f"\nEnter match number to edit (1-{len(matches_to_show)}) or '0' to go back: ")).strip()
        if get_cmd(idx_raw) == '0' or get_cmd(idx_raw) == 'back':
            return False

        idx_val = int(idx_raw) - 1
        if 0 <= idx_val < len(matches_to_show):
            match_to_edit = matches_to_show[idx_val]
            try:
                global_idx = history.index(match_to_edit)
            except ValueError:
                print(">>> ERROR: Could not find match in global history list.")
                return False
        else:
            print(">>> Number out of range.")
            return False

        print(f"\nEditing Match #{global_idx + 1}: {match_to_edit.get('t1', {}).get('name')} vs {match_to_edit.get('t2', {}).get('name')} ({match_to_edit.get('date', 'N/A')})")
        print(f"  Current Environment: {match_to_edit.get('env', 'N/A')}")
        print(f"  Current Event: {match_to_edit.get('event', 'N/A')}")
        print(f"  Current Tier: {match_to_edit.get('tier', 'N/A')}")
        print(f"  Current Grand Final: {match_to_edit.get('grand_final', False)}")

        changes_made = False

        env_choice = get_cmd(check_cmd(input("\nNew Environment (1=Online, 2=LAN, skip=unchanged): ")))
        if env_choice not in ['skip', '']:
            env_map = {'1': 'ONLINE', '2': 'LAN'}
            if env_choice in env_map:
                history[global_idx]['env'] = env_map[env_choice]
                mark_unsaved()
                print(f"  [OK] Environment updated to {history[global_idx]['env']}")
                changes_made = True
            else:
                print("  Invalid environment, keeping current.")

        event_choice = get_cmd(check_cmd(input("New Event Name (skip=unchanged): ")))
        if event_choice not in ['skip', '']:
            history[global_idx]['event'] = event_choice
            print(f"  [OK] Event updated to {history[global_idx]['event']}")
            changes_made = True

        tier_choice = get_cmd(check_cmd(input("New Tier (S+/S/A/B/C/D, skip=unchanged): ")).strip().upper())
        if tier_choice not in ['skip', '']:
            valid_tiers = ['S+', 'S', 'A', 'B', 'C', 'D', 'E']
            if tier_choice in valid_tiers:
                history[global_idx]['tier'] = tier_choice
                print(f"  [OK] Tier updated to {history[global_idx]['tier']}")
                changes_made = True
            else:
                print("  Invalid tier, keeping current.")

        gf_choice = get_cmd(check_cmd(input("Grand Final? (y/n/skip): ")).strip().lower())
        if gf_choice not in ['skip', '']:
            history[global_idx]['grand_final'] = (gf_choice == 'y')
            print(f"  [OK] Grand Final set to {history[global_idx]['grand_final']}")
            changes_made = True

        if changes_made:
            print("\n>>> Saving edited match to disk...")
            save_success = save_all()
            
            if save_success:
                print("\n>>> SUCCESS: Match details updated and saved.")
                
                resim = get_cmd(check_cmd(input("\nResimulate ALL matches now to apply changes? (y/n): ")))
                if resim == 'y':
                    print(">>> Starting full resimulation...")
                    _do_resimulation(history, verbose=True)
                    print(">>> Resimulation complete!")
                    return True
                else:
                    print(">>> Changes saved. Resimulate later from Match History menu option 3.")
                    return False
            else:
                print("\n>>> WARNING: Changes made but save failed! Do not resimulate.")
                return False
        else:
            print("\n>>> No changes made.")
            return False

    except ValueError:
        print(">>> Invalid input.")
        return False
    except Exception as e:
        print(f">>> ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

# =============================================================================
# === MATCH SIMULATION & COMPARISON ===
# =============================================================================

def simulate_match() -> None:
    """Simulate all possible outcomes for a match format without saving to history."""
    print_menu(
        "SIMULATE MATCH",
        [
            ("·", "Displays rating impact for every possible scoreline"),
        ],
    )
    
    t1_raw = check_cmd(input("Team 1: "))
    if get_cmd(t1_raw) in ['back', '0']:
        return
    t1 = find_team(t1_raw)
    if not t1:
        print("  [!] Team not found.")
        return
    
    t2_raw = check_cmd(input("Team 2: "))
    if get_cmd(t2_raw) in ['back', '0']:
        return
    t2 = find_team(t2_raw)
    if not t2:
        print("  [!] Team not found.")
        return
    
    if t1 == t2:
        print("  [!] Cannot simulate match against the same team.")
        return

    # Event tier auto-select
    tier_raw = 'A'
    use_event = get_cmd(check_cmd(input("\n  Select tier from a saved event? (y/n): ")))
    if use_event == 'y':
        if event_tiers:
            # Sort by most recent match date, then alphabetically
            def event_recency(ev):
                dates = [m.get('date', '') for m in history if m.get('event') == ev and m.get('date')]
                return max(dates) if dates else ''
            events_list = sorted(event_tiers.keys(), key=lambda ev: event_recency(ev), reverse=True)
            print("\n  Saved Events:")
            for i, ev in enumerate(events_list, 1):
                print(f"  [{i:>2}] {ev:<40} Tier: {event_tiers[ev]}")
            raw_ev = check_cmd(input("\n  Enter number or partial name: ")).strip()
            selected_ev = None
            if raw_ev.isdigit():
                idx = int(raw_ev) - 1
                if 0 <= idx < len(events_list):
                    selected_ev = events_list[idx]
            else:
                matches_ev = [ev for ev in events_list if raw_ev.lower() in ev.lower()]
                if len(matches_ev) == 1:
                    selected_ev = matches_ev[0]
                elif len(matches_ev) > 1:
                    for i, ev in enumerate(matches_ev, 1):
                        print(f"  [{i}] {ev}")
                    sub = check_cmd(input("  Select: ")).strip()
                    if sub.isdigit() and 1 <= int(sub) <= len(matches_ev):
                        selected_ev = matches_ev[int(sub) - 1]
            if selected_ev:
                tier_raw = event_tiers[selected_ev]
                print(f"  Using tier '{tier_raw}' from '{selected_ev}'")
            else:
                print("  Event not found — defaulting to manual entry.")
                use_event = 'n'
        else:
            print("  No saved events found.")
            use_event = 'n'

    if use_event != 'y':
        tier_raw = check_cmd(input("  Tier (S+/S/A/B/R/C/D): ")).strip().upper()
        if get_cmd(tier_raw) == 'back':
            return
        if tier_raw not in ['S+', 'S', 'A', 'B', 'C', 'D', 'E']:
            tier_raw = 'A'

    # Format selection
    print("\n  Format:  1. Best of 1   2. Best of 3   3. Best of 5")
    fmt_choice = get_cmd(check_cmd(input("  Select: ")))
    if fmt_choice in ['back', '0']:
        return
    formats = {'1': 1, '2': 3, '3': 5}
    bo = formats.get(fmt_choice)
    if not bo:
        print("  [!] Invalid format.")
        return

    # Possible scorelines — (t1_maps, t2_maps)
    wins_needed = (bo // 2) + 1
    scorelines = []
    for loser in range(0, wins_needed):
        scorelines.append((wins_needed, loser))   # t1 wins
        scorelines.append((loser, wins_needed))   # t2 wins

    print("  Environment:  1. Online   2. LAN")
    env_choice = get_cmd(check_cmd(input("  Select: ")))
    if env_choice == 'back':
        return
    env_raw = {'1': 'ONLINE', '2': 'LAN'}.get(env_choice, 'LAN')

    gf_raw = get_cmd(check_cmd(input("  Grand Final? (y/n): ")))
    is_gf = gf_raw == 'y'

    days_raw = check_cmd(input("  Days since last match (0 for today): "))
    try:
        days_inactive = int(days_raw)
    except:
        days_inactive = 0

    # Base ratings + depreciation
    p1 = teams[t1]
    p2 = teams[t2]
    p1d = calculate_depreciation(p1, days_inactive, t1) if days_inactive > DEPRECIATION_THRESHOLD else p1
    p2d = calculate_depreciation(p2, days_inactive, t2) if days_inactive > DEPRECIATION_THRESHOLD else p2

    form1 = calculate_form(t1, n=15, history=history)
    form2 = calculate_form(t2, n=15, history=history)
    form_adj_1 = (form1[1] - 50) if form1 else 0
    form_adj_2 = (form2[1] - 50) if form2 else 0

    t1_prov = is_provisional(t1)
    t2_prov = is_provisional(t2)

    old_order = [t for t in get_sorted_rankings(include_archived=True) if not is_provisional(t)]
    r1_old = old_order.index(t1) + 1 if t1 in old_order else '?'
    r2_old = old_order.index(t2) + 1 if t2 in old_order else '?'

    # Win probability per map (Elo formula, with form adjustment)
    p_map = 1 / (1 + 10 ** ((p2d - p1d) / 400))

    # Series win probabilities per scoreline
    def scoreline_prob(s1: int, s2: int) -> float:
        """Probability of exactly this scoreline occurring."""
        w, l = max(s1, s2), min(s1, s2)
        p = p_map if s1 > s2 else (1 - p_map)
        q = 1 - p
        wins_needed = w
        # Negative binomial: last game must be a win, previous (w-1) wins in (w+l-1) games
        from math import comb
        return comb(w + l - 1, l) * (p ** wins_needed) * (q ** l)

    # Overall series win probability
    series_win_prob = sum(scoreline_prob(s1, s2) for s1, s2 in scorelines if s1 > s2)

    fmt_shift = lambda s: f"(+{s})" if s > 0 else (f"({s})" if s < 0 else "(──)")
    fmt_pts   = lambda a, b: f"({int(round(a-b)):>+3})" if a != b else "( ─ )"

    bo_label = f"Best of {bo}"
    stage_label = "Grand Final  |  " if is_gf else ""
    print(f"\n{'═'*68}")
    print(f"  SIMULATION: {t1}  vs  {t2}  [{bo_label}]")
    print(f"  Tier: {tier_raw}  |  Env: {env_raw}  |  {stage_label}Ratings: {int(p1d)} vs {int(p2d)}")
    if days_inactive > DEPRECIATION_THRESHOLD:
        print(f"  [Depreciation: {t1} {int(p1)}→{int(p1d)}  |  {t2} {int(p2)}→{int(p2d)}]")
    print(f"  Series Win Probability:  {t1} {series_win_prob*100:.1f}%  |  {t2} {(1-series_win_prob)*100:.1f}%")
    print(f"{'─'*68}")
    print(f"  {'Score':<14}  {'%':>5}  {'Team':<22}  {'Before':>6}  {'After':>6}  {'Δ':>6}  Rank")
    print(f"{'─'*68}")

    for s1, s2 in scorelines:
        map_diff = abs(s1 - s2)
        prob = scoreline_prob(s1, s2)

        new_p1_raw = calculate_points(p1d, p2d, 1 if s1 > s2 else 0, map_diff, tier_raw, env_raw, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
        new_p2_raw = calculate_points(p2d, p1d, 1 if s2 > s1 else 0, map_diff, tier_raw, env_raw, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)

        if t1_prov:
            k1 = get_provisional_k(t1)
            new_p1 = min(max(RATING_FLOOR, p1d + (new_p1_raw - p1d) * k1), RATING_CAP)
        else:
            new_p1 = new_p1_raw
        if t2_prov:
            k2 = get_provisional_k(t2)
            new_p2 = min(max(RATING_FLOOR, p2d + (new_p2_raw - p2d) * k2), RATING_CAP)
        else:
            new_p2 = new_p2_raw

        sim = dict(teams)
        sim[t1] = new_p1
        sim[t2] = new_p2
        sim_order = sorted([t for t in sim if not is_provisional(t)], key=lambda x: sim[x], reverse=True)
        r1_new = sim_order.index(t1) + 1 if t1 in sim_order else '?'
        r2_new = sim_order.index(t2) + 1 if t2 in sim_order else '?'
        sh1 = (r1_old - r1_new) if isinstance(r1_old, int) and isinstance(r1_new, int) else 0
        sh2 = (r2_old - r2_new) if isinstance(r2_old, int) and isinstance(r2_new, int) else 0

        score_label = f"{t1} {s1}-{s2} {t2}"
        print(f"\n  {score_label}  ({prob*100:.1f}%)")
        print(f"  {'':14}  {'':>5}  {t1:<22}  {int(p1d):>6}  {int(new_p1):>6}  {fmt_pts(new_p1, p1d):>6}  #{r1_old} → #{r1_new} {fmt_shift(sh1)}")
        print(f"  {'':14}  {'':>5}  {t2:<22}  {int(p2d):>6}  {int(new_p2):>6}  {fmt_pts(new_p2, p2d):>6}  #{r2_old} → #{r2_new} {fmt_shift(sh2)}")

    print(f"\n{'═'*68}")
        
def compare_teams() -> None:
    """
    Display detailed head-to-head stats between two teams.
    """
    print("\n--- Compare Teams ---")
    
    t1_raw = check_cmd(input("Team 1: "))
    if get_cmd(t1_raw) in ['back', '0']:
        return
    t1 = find_team(t1_raw)
    if not t1:
        print("  [!] Team not found.")
        return

    t2_raw = check_cmd(input("Team 2: "))
    if get_cmd(t2_raw) in ['back', '0']:
        return
    t2 = find_team(t2_raw)
    if not t2:
        print("  [!] Team not found.")
        return
    
    if t1 == t2:
        print("  [!] Cannot compare a team against itself.")
        return

    ranked = get_sorted_rankings(include_archived=True)
    r1 = ranked.index(t1) + 1
    r2 = ranked.index(t2) + 1
    p1, p2 = teams[t1], teams[t2]
    pk1 = peak_ratings.get(t1, {})
    pk2 = peak_ratings.get(t2, {})

    h2h = [m for m in history if
           (m.get('t1', {}).get('name') == t1 and m.get('t2', {}).get('name') == t2) or
           (m.get('t1', {}).get('name') == t2 and m.get('t2', {}).get('name') == t1)]
    t1_wins = sum(1 for m in h2h if
                  (m.get('t1', {}).get('name') == t1 and m.get('t1', {}).get('score', 0) > m.get('t2', {}).get('score', 0)) or
                  (m.get('t2', {}).get('name') == t1 and m.get('t2', {}).get('score', 0) > m.get('t1', {}).get('score', 0)))
    t2_wins = len(h2h) - t1_wins
    
    t1_maps = 0
    t2_maps = 0
    for m in h2h:
        if m.get('t1', {}).get('name') == t1:
            t1_maps += m.get('t1', {}).get('score', 0)
            t2_maps += m.get('t2', {}).get('score', 0)
        else:
            t1_maps += m.get('t2', {}).get('score', 0)
            t2_maps += m.get('t1', {}).get('score', 0)

    t1_opponents = set()
    t2_opponents = set()
    for m in history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == t1: t1_opponents.add(t2_name)
        elif t2_name == t1: t1_opponents.add(t1_name)
        
        if t1_name == t2: t2_opponents.add(t2_name)
        elif t2_name == t2: t2_opponents.add(t1_name)
    
    common_opponents = t1_opponents.intersection(t2_opponents)
    common_wins_t1 = 0
    common_wins_t2 = 0
    for opp in common_opponents:
        for m in history:
            m_t1 = m.get('t1', {}).get('name')
            m_t2 = m.get('t2', {}).get('name')
            m_s1 = m.get('t1', {}).get('score', 0)
            m_s2 = m.get('t2', {}).get('score', 0)
            
            if (m_t1 == t1 and m_t2 == opp and m_s1 > m_s2) or \
               (m_t2 == t1 and m_t1 == opp and m_s2 > m_s1):
                common_wins_t1 += 1
            elif (m_t1 == t2 and m_t2 == opp and m_s1 > m_s2) or \
                 (m_t2 == t2 and m_t1 == opp and m_s2 > m_s1):
                common_wins_t2 += 1

    w = 24
    print(f"\n{'='*(w*2+3)}")
    print(f"  {'Stat':<18} {t1:>{w}} {t2:>{w}}")
    print(f"{'='*(w*2+3)}")
    print(f"  {'Points':<18} {int(p1):>{w}} {int(p2):>{w}}")
    print(f"  {'Rank':<18} {'#'+str(r1):>{w}} {'#'+str(r2):>{w}}")
    pk1_str = f"{int(pk1.get('points',0))} ({pk1.get('date','N/A')})" if pk1 else "N/A"
    pk2_str = f"{int(pk2.get('points',0))} ({pk2.get('date','N/A')})" if pk2 else "N/A"
    print(f"  {'Peak Rating':<18} {pk1_str:>{w}} {pk2_str:>{w}}")
    
    f1 = calculate_form(t1, n=15, history=history)
    f2 = calculate_form(t2, n=15, history=history)
    f1_str = f"{f1[0]} {f1[1]} ({f1[2]})" if f1 else "N/A"
    f2_str = f"{f2[0]} {f2[1]} ({f2[2]})" if f2 else "N/A"
    print(f"  {'Form':<18} {f1_str:>{w}} {f2_str:>{w}}")

    t1_trend = get_team_trend(t1)
    t2_trend = get_team_trend(t2)
    if t1_trend and t2_trend:
        trend1, diff1 = t1_trend
        trend2, diff2 = t2_trend
        arrow1 = "^" if trend1 == "up" else ("v" if trend1 == "down" else "-")
        arrow2 = "^" if trend2 == "up" else ("v" if trend2 == "down" else "-")
    else:
        arrow1 = arrow2 = "-"
    print(f"  {'Trend':<18} {arrow1:>{w}} {arrow2:>{w}}")

    print(f"{'='*(w*2+3)}")
    print(f"  {'H2H Matches':<18} {len(h2h):>{w}}")
    print(f"  {'H2H Wins':<18} {t1_wins:>{w}} {t2_wins:>{w}}")
    print(f"  {'H2H Map Diff':<18} {t1_maps - t2_maps:>{w}} {t2_maps - t1_maps:>{w}}")
    print(f"{'='*(w*2+3)}")
    print(f"  {'Common Opponents':<18} {len(common_opponents):>{w}}")
    print(f"  {'Wins vs Common':<18} {common_wins_t1:>{w}} {common_wins_t2:>{w}}")
    print(f"{'='*(w*2+3)}")

def view_team_rating_graph() -> None:
    """Display tkinter line graph of a single team's rating history."""
    print("\n=== TEAM RATING GRAPH ===")

    team_raw = check_cmd(input("Enter team name: ")).strip()
    if get_cmd(team_raw) in ['back', '0']:
        return
    team = find_team(team_raw)
    if not team:
        print(">>> Team not found.")
        return

    all_team_matches = []
    for m in history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team:
            all_team_matches.append(('t1', m))
        elif t2_name == team:
            all_team_matches.append(('t2', m))

    if len(all_team_matches) < 2:
        print(">>> Not enough match history to generate graph (minimum 2 matches).")
        return
    all_team_matches.sort(key=lambda x: x[1].get('date', ''))

    start_date, end_date = pick_date_range()
    if start_date is None or end_date is None:
        print("\n  >>> Date selection cancelled.")
        return

    def _in_range(date_str):
        try:
            return start_date <= datetime.strptime(date_str[:10], "%Y-%m-%d").date() <= end_date
        except Exception:
            return False

    team_matches = [(side, m) for side, m in all_team_matches if _in_range(m.get('date', ''))]
    if len(team_matches) < 2:
        print(">>> Not enough matches in selected date range (minimum 2).")
        return

    first_side, first_match = team_matches[0]
    start_rating = first_match.get(first_side, {}).get('pts_before') or STARTING_TEAMS.get(team, 1000)
    start_point_date = (datetime.strptime(team_matches[0][1].get('date', '')[:10], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    timeline = []
    all_dates = set()
    timeline.append((start_point_date, start_rating, 'start', None))
    all_dates.add(start_point_date)

    for side, m in team_matches:
        match_date = m.get('date', 'N/A')[:10]
        pts_after  = m.get(side, {}).get('pts_after')
        pts_before = m.get(side, {}).get('pts_before')
        if pts_after is not None:
            timeline.append((match_date, pts_after, 'match', pts_before))
            all_dates.add(match_date)

    today = datetime.now().date()
    end_date_str = end_date.strftime("%Y-%m-%d")
    if end_date >= today:
        current_rating = teams.get(team, 1000)
        timeline.append((end_date_str, current_rating, 'current', None))
    else:
        last_pts = timeline[-1][1]
        current_rating = last_pts
        timeline.append((end_date_str, last_pts, 'current', None))
    all_dates.add(end_date_str)

    peak_rating = max(pts for _, pts, _, _ in timeline if pts is not None)
    peak_date = next((d for d, pts, _, _ in timeline if pts == peak_rating), None)
    all_dates = sorted(all_dates)

    if len(all_dates) < 2:
        print(">>> Not enough date points to generate graph.")
        return

    try:
        _show_single_team_graph(team, timeline, all_dates, current_rating, peak_rating, peak_date)
    except Exception as e:
        logger.error(f"Graph failed: {e}")
        print(f"\n  [!] Graph failed: {e}")
        import traceback
        traceback.print_exc()

def view_team_form_graph() -> None:
    """Display tkinter line graph of a single team's form history."""
    print("\n=== TEAM FORM GRAPH ===")

    team_raw = check_cmd(input("Enter team name: ")).strip()
    if get_cmd(team_raw) in ['back', '0']:
        return
    team = find_team(team_raw)
    if not team:
        print(">>> Team not found.")
        return

    full_form_timeline = build_form_timeline(team)
    if not full_form_timeline or len(full_form_timeline) < 2:
        print(">>> Not enough match history to generate form graph (minimum 4 matches required).")
        return

    start_date, end_date = pick_date_range()
    if start_date is None or end_date is None:
        print("\n  >>> Date selection cancelled.")
        return

    def _in_range(date_str):
        try:
            return start_date <= datetime.strptime(date_str[:10], "%Y-%m-%d").date() <= end_date
        except Exception:
            return False

    form_timeline = [(d, score, grade, idx) for d, score, grade, idx in full_form_timeline if _in_range(d)]
    if len(form_timeline) < 2:
        print(">>> Not enough form data points in selected date range (minimum 2).")
        return

    all_dates = sorted(set(d for d, _, _, _ in form_timeline))
    end_date_str = end_date.strftime("%Y-%m-%d")
    if end_date_str not in all_dates:
        all_dates = sorted(set(all_dates) | {end_date_str})

    if len(all_dates) < 2:
        print(">>> Not enough date points to generate graph.")
        return

    current_form = calculate_form(team, n=15, history=history)

    try:
        _show_single_team_form_graph(team, form_timeline, all_dates, current_form)
    except Exception as e:
        logger.error(f"Graph failed: {e}")
        print(f"\n  [!] Graph failed: {e}")
        import traceback
        traceback.print_exc()

def view_team_combined_graph() -> None:
    """Display combined rating AND form graph for a single team."""
    print("\n=== TEAM COMBINED GRAPH ===")

    team_raw = check_cmd(input("Enter team name: ")).strip()
    if get_cmd(team_raw) in ['back', '0']:
        return
    team = find_team(team_raw)
    if not team:
        print(">>> Team not found.")
        return

    all_team_matches = []
    for m in history:
        t1_name = m.get('t1', {}).get('name')
        t2_name = m.get('t2', {}).get('name')
        if t1_name == team:
            all_team_matches.append(('t1', m))
        elif t2_name == team:
            all_team_matches.append(('t2', m))

    if len(all_team_matches) < 2:
        print(">>> Not enough match history to generate graph (minimum 2 matches).")
        return
    all_team_matches.sort(key=lambda x: x[1].get('date', ''))

    full_form_timeline = build_form_timeline(team)

    start_date, end_date = pick_date_range()
    if start_date is None or end_date is None:
        print("\n  >>> Date selection cancelled.")
        return

    def _in_range(date_str):
        try:
            return start_date <= datetime.strptime(date_str[:10], "%Y-%m-%d").date() <= end_date
        except Exception:
            return False

    team_matches = [(side, m) for side, m in all_team_matches if _in_range(m.get('date', ''))]
    if len(team_matches) < 2:
        print(">>> Not enough matches in selected date range (minimum 2).")
        return

    first_side, first_match = team_matches[0]
    start_rating = first_match.get(first_side, {}).get('pts_before') or STARTING_TEAMS.get(team, 1000)
    start_point_date = (datetime.strptime(team_matches[0][1].get('date', '')[:10], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    rating_timeline = []
    all_dates = set()
    rating_timeline.append((start_point_date, start_rating, 'start', None))
    all_dates.add(start_point_date)

    for side, m in team_matches:
        match_date = m.get('date', 'N/A')[:10]
        pts_after  = m.get(side, {}).get('pts_after')
        pts_before = m.get(side, {}).get('pts_before')
        if pts_after is not None:
            rating_timeline.append((match_date, pts_after, 'match', pts_before))
            all_dates.add(match_date)

    today = datetime.now().date()
    end_date_str = end_date.strftime("%Y-%m-%d")
    if end_date >= today:
        current_rating = teams.get(team, 1000)
        rating_timeline.append((end_date_str, current_rating, 'current', None))
    else:
        last_pts = rating_timeline[-1][1]
        current_rating = last_pts
        rating_timeline.append((end_date_str, last_pts, 'current', None))
    all_dates.add(end_date_str)

    form_timeline = None
    if full_form_timeline:
        form_timeline = [(d, score, grade, idx) for d, score, grade, idx in full_form_timeline if _in_range(d)]
        if len(form_timeline) < 2:
            form_timeline = None
        else:
            for date, score, grade, _ in form_timeline:
                all_dates.add(date)

    all_dates = sorted(all_dates)
    if len(all_dates) < 2:
        print(">>> Not enough date points to generate graph.")
        return

    peak_rating = max(pts for _, pts, _, _ in rating_timeline if pts is not None)
    peak_date = next((d for d, pts, _, _ in rating_timeline if pts == peak_rating), None)
    current_form = calculate_form(team, n=15, history=history)

    try:
        _show_combined_graph(team, rating_timeline, form_timeline, all_dates, current_rating, peak_rating, peak_date, current_form)
    except Exception as e:
        logger.error(f"Graph failed: {e}")
        print(f"\n  [!] Graph failed: {e}")
        import traceback
        traceback.print_exc()

def _draw_depreciation_curve(
    canvas,
    x1,
    y1,
    rating1,
    x2,
    y2,
    rating2,
    color,
    margin_top,
    graph_height,
    min_y,
    range_y,
    start_date,
    end_date,
    team_name,
):
    """
    Draw the rating line that connects two matches.

    Three gap‑size cases are supported:

    • 0‑1 day  → straight line (no markers).  
    • 2‑7 days → flat segment for the first (gap‑1) days, then a
      diagonal for the last day. **One circle** is drawn at the flat‑end
      (the exact point where the line turns diagonal).  
    • > 7 days → a flat part for the first 7 days followed by the full
      depreciation curve. **One circle** is drawn at day 7 (the transition
      from the flat part to the curve).  No marker is drawn at the last
      point of the curve because the next match already has its own
      marker; drawing another circle would duplicate the point.

    The function does **not** draw a marker for the non‑depreciated rating.
    """
    from datetime import datetime

    # --------------------------------------------------------------
    # 1️⃣  Parse dates and compute the day gap
    # --------------------------------------------------------------
    d_start = datetime.strptime(start_date, "%Y-%m-%d")
    d_end   = datetime.strptime(end_date,   "%Y-%m-%d")
    days_gap = (d_end - d_start).days          # guaranteed ≥ 0

    # --------------------------------------------------------------
    # 2️⃣  Gap 0‑1 day → simple straight line
    # --------------------------------------------------------------
    if days_gap <= 1:
        canvas.create_line(x1, y1, x2, y2, fill=color, width=2)
        return

    # --------------------------------------------------------------
    # 3️⃣  Gap 2‑7 days → flat then diagonal, with flat‑end marker
    # --------------------------------------------------------------
    if days_gap <= DEPRECIATION_THRESHOLD:
        # flat segment (first gap‑1 days)
        x_flat_end = x1 + ((x2 - x1) * ((days_gap - 1) / days_gap))
        canvas.create_line(x1, y1, x_flat_end, y1,
                           fill=color, width=2)      # flat part
        canvas.create_line(x_flat_end, y1, x2, y2,
                           fill=color, width=2)      # diagonal part

        # **flat‑end transition marker**
        canvas.create_oval(x_flat_end - 4, y1 - 4,
                           x_flat_end + 4, y1 + 4,
                           fill=color, outline='white', width=1)
        return

    # --------------------------------------------------------------
    # 4️⃣  Gap > 7 days → day‑7 marker + depreciation curve
    # --------------------------------------------------------------
    # day‑7 point (still at the old rating)
    x_day7 = x1 + ((x2 - x1) * (DEPRECIATION_THRESHOLD / days_gap))
    y_day7 = y1
    canvas.create_line(x1, y1, x_day7, y_day7,
                       fill=color, width=2)

    # threshold transition marker
    canvas.create_oval(x_day7 - 4, y_day7 - 4,
                       x_day7 + 4, y_day7 + 4,
                       fill=color, outline='white', width=1)

    # ---- build the depreciation‑curve points (day 8 … last day) ----
    dep_points = [(x_day7, y_day7, rating1)]

    for day in range(DEPRECIATION_THRESHOLD + 1, days_gap + 1):
        daily_rating = calculate_depreciation(rating1, day, team_name)
        day_x = x1 + ((x2 - x1) * (day / days_gap))
        if day == days_gap:
            daily_y = y2   # snap to exact arrival Y
        else:
            daily_y = margin_top + graph_height - (
                (daily_rating - min_y) / range_y * graph_height
            )
        dep_points.append((day_x, daily_y, daily_rating))

    # ---- draw the depreciation curve ---------------------------------
    for i in range(len(dep_points) - 1):
        canvas.create_line(dep_points[i][0], dep_points[i][1],
                           dep_points[i + 1][0], dep_points[i + 1][1],
                           fill=color, width=2)

def _show_single_team_graph(team, timeline, all_dates,
                            current_rating, peak_rating, peak_date):
    """
    Graph the rating history of a single team (rating only).
    Includes the depreciation curve and shows the **depreciated** current rating.
    """
    import tkinter as tk
    from datetime import datetime, timedelta

    # ------------------------------------------------------------------
    #   Window and basic sizing
    # ------------------------------------------------------------------
    root = tk.Tk()
    root.title(f"{team} – Rating Over Time")
    root.configure(bg='#1a1a1a')

    # ------------------------------------------------------------------
    #   Prepare dates (add a one‑day buffer at the start)
    # ------------------------------------------------------------------
    date_objects = [datetime.strptime(d, "%Y-%m-%d") for d in all_dates]
    first_date = date_objects[0]
    buffer_start = first_date - timedelta(days=1)

    # Add the buffer date to the axis so the graph always has a little space.
    all_dates_with_buffer = [buffer_start.strftime("%Y-%m-%d")] + all_dates
    date_objects_with_buffer = [buffer_start] + date_objects

    total_days = (date_objects_with_buffer[-1] -
                  date_objects_with_buffer[0]).days + 1

    # ------------------------------------------------------------------
    #   Y‑range (add a little padding)
    # ------------------------------------------------------------------
    ratings = [pts for _, pts, _, _ in timeline if pts is not None]
    min_rating = min(ratings)
    max_rating = max(ratings)

    rating_range = max_rating - min_rating
    pad = rating_range * 0.1 if rating_range > 0 else 50
    rating_min = max(0, min_rating - pad)
    rating_max = min(RATING_CAP, max_rating + pad)

    # round to the nearest 50 for a tidy grid
    rating_min = (int(rating_min) // 50) * 50
    rating_max = ((int(rating_max) // 50) + 1) * 50

    # ------------------------------------------------------------------
    #   Compute *depreciated* rating for “today”
    # ------------------------------------------------------------------
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_match_date = None
    for d, _, pt_type, _ in timeline:
        if pt_type == 'match':
            last_match_date = d

    depreciated = current_rating
    days_inactive = 0
    if last_match_date:
        last_dt = datetime.strptime(last_match_date, "%Y-%m-%d")
        today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        days_inactive = (today_dt - last_dt).days
        if days_inactive > DEPRECIATION_THRESHOLD:
            depreciated = calculate_depreciation(
                current_rating, days_inactive, team
            )

    # ------------------------------------------------------------------
    #   Pack everything for drawing
    # ------------------------------------------------------------------
    graph_data = {
        'team': team,
        'total_days': total_days,
        'all_dates': all_dates_with_buffer,
        'date_objects': date_objects_with_buffer,
        'timeline': timeline,
        'current_rating': current_rating,
        'depreciated_rating': depreciated,
        'days_inactive': days_inactive,
        'peak_rating': peak_rating,
        'peak_date': peak_date,
        'rating_min': rating_min,
        'rating_max': rating_max,
    }

    team_color = '#3cb44b'      # default line colour

    # ------------------------------------------------------------------
    #   Canvas
    # ------------------------------------------------------------------
    width, height = 1200, 600
    root.geometry(f"{width}x{height}")

    canvas = tk.Canvas(root, bg='#1a1a1a', highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    close_btn = [None]          # mutable holder for the Close button

    # ------------------------------------------------------------------
    #   Resize handling – redraw on every window size change
    # ------------------------------------------------------------------
    def on_resize(event):
        canvas.configure(scrollregion=(0, 0, event.width, event.height))
        draw_graph(event.width, event.height)
        if close_btn[0]:
            close_btn[0].place(x=event.width // 2 - 40,
                               y=event.height - 50)

    canvas.bind("<Configure>", on_resize)

    # ------------------------------------------------------------------
    #   Main drawing routine
    # ------------------------------------------------------------------
    def draw_graph(cw, ch):
        canvas.delete("all")

        # ---- margins -------------------------------------------------
        m_left, m_top, m_bottom = 80, 60, 130
        m_right = 100
        g_width = int((cw - m_left - m_right) * 0.95)
        g_right = m_left + g_width
        g_height = max(100, ch - m_top - m_bottom)

        px_day = g_width / graph_data['total_days'] if \
                 graph_data['total_days'] > 1 else g_width
        r_min = graph_data['rating_min']
        r_max = graph_data['rating_max']
        r_range = r_max - r_min

        # ---- title --------------------------------------------------
        canvas.create_text(cw // 2, 25,
                           text=f"{graph_data['team']} – Rating Over Time",
                           font=('Arial', 14, 'bold'), fill='white')

        # ---- subtitle (depreciation info) ---------------------------
        dep_info = ""
        if graph_data['days_inactive'] > DEPRECIATION_THRESHOLD:
            loss = int(graph_data['current_rating'] -
                       graph_data['depreciated_rating'])
            dep_info = f" | Dep: -{loss} ({graph_data['days_inactive']}d)"
        stats = (f"Current: {int(graph_data['current_rating'])}{dep_info}  |  "
                 f"Peak: {int(graph_data['peak_rating'])}  |  "
                 f"Range: {graph_data['all_dates'][1]} to "
                 f"{graph_data['all_dates'][-1]}")
        canvas.create_text(cw // 2, 45,
                           text=stats,
                           font=('Arial', 9), fill='#888888')

        # ---- date → x map -----------------------------------------
        date_to_x = {}
        start_dt = graph_data['date_objects'][0]
        for i, dt in enumerate(graph_data['date_objects']):
            days_off = (dt - start_dt).days
            date_to_x[graph_data['all_dates'][i]] = (
                m_left + days_off * px_day
            )
        end_x = date_to_x.get(graph_data['all_dates'][-1], g_right)

        # ---- horizontal grid lines with styled labels ---------------
        for i in range(6):
            y_val = r_min + i * (r_range / 5)
            y_pos = m_top + g_height - (i / 5 * g_height)
            canvas.create_line(m_left, y_pos, end_x, y_pos,
                               fill='#2a2a2a', dash=(5, 5))
            canvas.create_text(m_left - 8, y_pos,
                               text=str(int(y_val)),
                               font=('Arial', 8, 'bold'), fill='#aaaaaa',
                               anchor='e')

        # ---- elite threshold reference line -------------------------
        if r_min <= ELITE_THRESHOLD <= r_max:
            elite_y = m_top + g_height - (
                (ELITE_THRESHOLD - r_min) / r_range * g_height
            )
            canvas.create_line(m_left, elite_y, end_x, elite_y,
                               fill='#4363d8', dash=(6, 3), width=1)
            canvas.create_text(m_left - 8, elite_y,
                               text=str(int(ELITE_THRESHOLD)),
                               font=('Arial', 8, 'bold'), fill='#4363d8',
                               anchor='e')
            canvas.create_text(end_x + 8, elite_y,
                               text="Elite",
                               font=('Arial', 8, 'bold'), fill='#4363d8',
                               anchor='w')

        # ---- axes --------------------------------------------------
        canvas.create_line(m_left, m_top,
                           m_left, m_top + g_height,
                           fill='white', width=1)
        canvas.create_line(m_left, m_top + g_height,
                           end_x, m_top + g_height,
                           fill='white', width=1)

        # ---- peak‑rating line ---------------------------------------
        peak_y = m_top + g_height - (
            (graph_data['peak_rating'] - r_min) / r_range * g_height
        )
        canvas.create_line(m_left, peak_y, end_x, peak_y,
                           fill='#FFD700', dash=(8, 4), width=1)
        canvas.create_text(end_x + 12, peak_y,
                           text=f"Peak: {int(graph_data['peak_rating'])}",
                           font=('Arial', 8, 'bold'), fill='#FFD700',
                           anchor='w')

        # ---- current‑rating line (depreciated) --------------------
        cur_y = m_top + g_height - (
            (graph_data['depreciated_rating'] - r_min) / r_range * g_height
        )
        canvas.create_line(m_left, cur_y, end_x, cur_y,
                           fill=team_color, dash=(8, 4), width=1)
        canvas.create_text(end_x + 12, cur_y,
                           text=f"Current: {int(graph_data['depreciated_rating'])}",
                           font=('Arial', 8, 'bold'), fill=team_color,
                           anchor='w')

        # ---- build canvas points from timeline (pts_after Y) -------
        # Only include 'match' entries — we handle start and end separately.
        points = []
        for d, rating, ptype, _ in graph_data['timeline']:
            if d in date_to_x and rating is not None and ptype == 'match':
                x = date_to_x[d]
                y = m_top + g_height - ((rating - r_min) / r_range * g_height)
                points.append((x, y, d, rating, ptype))
        points.sort(key=lambda p: p[0])

        # Deduplicate same-x points (keep last — mirrors elite graph)
        if points:
            unique = [points[0]]
            for p in points[1:]:
                if abs(p[0] - unique[-1][0]) < 1:
                    unique[-1] = p
                else:
                    unique.append(p)
            points = unique

        # Compute depreciated end-point (mirrors elite graph logic exactly)
        end_date_str = graph_data['all_dates'][-1]
        if points and end_date_str in date_to_x:
            last_rating = points[-1][3]
            last_date   = points[-1][2]
            try:
                days_to_end = (datetime.strptime(end_date_str, "%Y-%m-%d") -
                               datetime.strptime(last_date,    "%Y-%m-%d")).days
            except Exception:
                days_to_end = 0
            dep_rating = (calculate_depreciation(last_rating, days_to_end, team)
                          if days_to_end > DEPRECIATION_THRESHOLD else last_rating)
            y_end = m_top + g_height - ((dep_rating - r_min) / r_range * g_height)
            points.append((date_to_x[end_date_str], y_end, end_date_str, dep_rating, 'end'))

        # ---- start-rating line/marker -----------------------------------
        start_rating_entry = next(
            (r for _, r, pt, _ in graph_data['timeline'] if pt == 'start'), None
        )
        start_rating = (start_rating_entry if start_rating_entry is not None
                        else STARTING_TEAMS.get(team, 1000))
        start_x = m_left
        start_y = m_top + g_height - ((start_rating - r_min) / r_range * g_height)
        if points:
            canvas.create_line(start_x, start_y,
                               points[0][0], points[0][1],
                               fill=team_color, width=2)

        # ---- draw rating lines segment by segment (elite graph logic) ---
        if len(points) > 1:
            for i in range(len(points) - 1):
                x1, y1, d1, r1, t1 = points[i]
                x2, y2, d2, r2, t2 = points[i + 1]
                try:
                    days_gap = (datetime.strptime(d2, "%Y-%m-%d") -
                                datetime.strptime(d1, "%Y-%m-%d")).days
                except Exception:
                    days_gap = 0

                is_end = (t2 == 'end')

                if days_gap > DEPRECIATION_THRESHOLD:
                    # flat segment to threshold
                    x_thresh = x1 + (x2 - x1) * (DEPRECIATION_THRESHOLD / days_gap)
                    canvas.create_line(x1, y1, x_thresh, y1,
                                       fill=team_color, width=2)
                    # depreciation curve
                    dep_pts = [(x_thresh, y1, r1)]
                    dep_end = days_gap if is_end else days_gap - 1
                    if dep_end > DEPRECIATION_THRESHOLD:
                        for day in range(DEPRECIATION_THRESHOLD + 1, dep_end + 1):
                            dr = calculate_depreciation(r1, day, team)
                            dx = x1 + (x2 - x1) * (day / days_gap)
                            dy = m_top + g_height - ((dr - r_min) / r_range * g_height)
                            dep_pts.append((dx, dy, dr))
                    for j in range(len(dep_pts) - 1):
                        canvas.create_line(dep_pts[j][0], dep_pts[j][1],
                                           dep_pts[j+1][0], dep_pts[j+1][1],
                                           fill=team_color, width=2)
                    if not is_end:
                        # marker at curve end (day before match) — drawn BEFORE
                        # the connect line, exactly as the elite graph does it
                        trans_x, trans_y, trans_r = dep_pts[-1]
                        canvas.create_oval(trans_x-3, trans_y-3,
                                           trans_x+3, trans_y+3,
                                           fill=team_color, outline='white', width=1)
                        # connect curve tail → next match dot
                        canvas.create_line(trans_x, trans_y, x2, y2,
                                           fill=team_color, width=2)
                elif days_gap > 1:
                    # flat then diagonal (gap within threshold) — mirrors elite exactly
                    x_flat = x1 + (x2 - x1) * ((days_gap - 1) / days_gap)
                    canvas.create_line(x1, y1, x_flat, y1,
                                       fill=team_color, width=2)
                    if not is_end and abs(x_flat - x1) > 5:
                        canvas.create_oval(x_flat-3, y1-3, x_flat+3, y1+3,
                                           fill=team_color, outline='white', width=1)
                    canvas.create_line(x_flat, y1, x2, y2,
                                       fill=team_color, width=2)
                else:
                    canvas.create_line(x1, y1, x2, y2, fill=team_color, width=2)

        # ---- draw point markers ----------------------------------------
        for x, y, d, rating, ptype in points:
            if ptype == 'end':
                canvas.create_oval(x-5, y-5, x+5, y+5,
                                   fill='#1a1a1a', outline=team_color, width=2)
            elif rating == graph_data['peak_rating']:
                canvas.create_oval(x-5, y-5, x+5, y+5,
                                   fill='#1a1a1a', outline='#FFD700', width=2)
            elif ptype == 'match':
                canvas.create_oval(x-3, y-3, x+3, y+3,
                                   fill=team_color, outline='white', width=1)

        # ---- start-rating marker ----------------------------------------
        canvas.create_oval(start_x-5, start_y-5, start_x+5, start_y+5,
                           fill='#1a1a1a', outline='#4363d8', width=2)

        # ---- date‑axis labels (spaced out) -------------------------
        min_spacing = 100
        to_show = []
        sorted_pos = sorted(date_to_x.items(), key=lambda kv: kv[1])
        if sorted_pos:
            last_x = sorted_pos[-1][1] + min_spacing
            for d, x in reversed(sorted_pos):
                if last_x - x >= min_spacing:
                    to_show.append((x, d))
                    last_x = x
            to_show.reverse()
            if len(to_show) < 2 and len(sorted_pos) >= 2:
                to_show = [sorted_pos[-2], sorted_pos[-1]]

        base_y = m_top + g_height
        for x, d in to_show:
            canvas.create_text(x, base_y + 12,
                               text='|', font=('Arial', 8), fill='#888888')
            canvas.create_text(x, base_y + 28,
                               text=d, font=('Arial', 7),
                               fill='#888888', anchor='n')

        # ---- legend ------------------------------------------------
        leg_x = m_left
        leg_y = m_top + g_height + 50

        # Start dot
        canvas.create_oval(leg_x, leg_y, leg_x+10, leg_y+10,
                           fill='#1a1a1a', outline='#4363d8', width=2)
        canvas.create_text(leg_x+16, leg_y+5, text="Start of Range",
                           font=('Arial', 8), fill='#888888', anchor='w')

        # Peak dot
        canvas.create_oval(leg_x+140, leg_y, leg_x+150, leg_y+10,
                           fill='#1a1a1a', outline='#FFD700', width=2)
        canvas.create_text(leg_x+156, leg_y+5,
                           text=f"Peak  ({int(graph_data['peak_rating'])})",
                           font=('Arial', 8), fill='#888888', anchor='w')

        # Current/end dot
        canvas.create_oval(leg_x+280, leg_y, leg_x+290, leg_y+10,
                           fill='#1a1a1a', outline=team_color, width=2)
        dep_txt = (f"Current  ({int(graph_data['depreciated_rating'])})"
                   if graph_data['days_inactive'] <= DEPRECIATION_THRESHOLD
                   else f"Current  ({int(graph_data['depreciated_rating'])}  dep.)")
        canvas.create_text(leg_x+296, leg_y+5, text=dep_txt,
                           font=('Arial', 8), fill='#888888', anchor='w')

        # Match dot
        canvas.create_oval(leg_x+440, leg_y+2, leg_x+446, leg_y+8,
                           fill=team_color, outline='white', width=1)
        canvas.create_text(leg_x+452, leg_y+5, text="Match result",
                           font=('Arial', 8), fill='#888888', anchor='w')

        # Elite threshold line swatch (if visible)
        if r_min <= ELITE_THRESHOLD <= r_max:
            canvas.create_line(leg_x+560, leg_y+5, leg_x+575, leg_y+5,
                               fill='#4363d8', dash=(4, 2), width=1)
            canvas.create_text(leg_x+581, leg_y+5,
                               text=f"Elite  ({ELITE_THRESHOLD})",
                               font=('Arial', 8), fill='#4363d8', anchor='w')

        # Depreciation note
        if graph_data['days_inactive'] > DEPRECIATION_THRESHOLD:
            loss = int(graph_data['current_rating'] - graph_data['depreciated_rating'])
            canvas.create_text(leg_x, leg_y+22,
                               text=f"⚠ Inactive {graph_data['days_inactive']}d  —  depreciation: -{loss} pts",
                               font=('Arial', 8), fill='#aa8800', anchor='w')

        # ---- Close button -------------------------------------------
        if close_btn[0] is None:
            close_btn[0] = tk.Button(root, text="Close",
                                    command=root.destroy,
                                    bg='#404040', fg='white',
                                    font=('Arial', 10),
                                    padx=30, pady=5)
        close_btn[0].place(x=cw // 2 - 40,
                           y=ch - 45)

    # ------------------------------------------------------------------
    #   Initial draw
    # ------------------------------------------------------------------
    draw_graph(width, height)
    root.mainloop()

def _draw_form_segment(canvas, x1, y1, s1, x2, y2, s2,
                       tier_colors, thresholds, m_top, g_height,
                       f_min, f_range):
    """
    Draw a line between two form points (s1 → s2) and automatically split
    it at any tier‑boundary that it crosses.

    Parameters
    ----------
    canvas : tk.Canvas
    x1, y1 : float – pixel coordinates of the first point
    s1      : float – form score of the first point (0‑100)
    x2, y2 : float – pixel coordinates of the second point
    s2      : float – form score of the second point
    tier_colors : dict – tier → colour (e.g. {'S':'#FFD700', ...})
    thresholds  : list – tier thresholds in **descending** order, e.g.
                       [100, 85, 70, 55, 40, 0]
    m_top, g_height, f_min, f_range – graph geometry (same as in the
                                        original drawing routine)
    """
    # If both points are inside the same tier → one line, done.
    def tier_of(score):
        """Return the tier key that belongs to `score`."""
        if score >= 85:   return 'S'
        if score >= 70:   return 'A'
        if score >= 55:   return 'B'
        if score >= 40:   return 'C'
        return 'D'

    c1 = tier_colors[tier_of(s1)]
    c2 = tier_colors[tier_of(s2)]

    # Same colour → simple line
    if c1 == c2:
        canvas.create_line(x1, y1, x2, y2, fill=c1, width=2)
        return

    # The segment crosses at least one threshold.
    # Work from the higher score downwards (or upwards) and cut it each time
    # we hit a boundary.
    # --------------------------------------------------------------
    #   Build a list of crossing points (including the start & end)
    # --------------------------------------------------------------
    cross_pts = [(x1, y1, s1, c1)]          # start point with colour of its tier

    # Determine which thresholds are between s1 and s2.
    lo, hi = (s2, s1) if s1 > s2 else (s1, s2)   # lo < hi
    # thresholds are already sorted descending, so we iterate in that order.
    for thr in thresholds:
        if lo < thr <= hi:                     # the line hits this boundary
            # proportion of the way from the higher score to the lower score
            # (score_high - thr) / (score_high - score_low)
            if s1 > s2:   # descending
                t = (s1 - thr) / (s1 - s2)
            else:         # ascending
                t = (thr - s1) / (s2 - s1)

            # x‑coordinate of the intersection with the horizontal thresh‑line
            xc = x1 + t * (x2 - x1)
            # y‑coordinate is the same for every threshold (horizontal line)
            yc = m_top + g_height - ((thr - f_min) / f_range * g_height)

            # colour after we cross the threshold is the colour of the next tier
            after_thr = tier_of(thr - 0.01)   # just below the threshold
            col_after = tier_colors[after_thr]

            cross_pts.append((xc, yc, thr, col_after))

    cross_pts.append((x2, y2, s2, c2))          # final point

    # --------------------------------------------------------------
    #   Draw each sub‑segment with its own colour
    # --------------------------------------------------------------
    for i in range(len(cross_pts) - 1):
        x_start, y_start, _, col_start = cross_pts[i]
        x_end,   y_end,   _, _          = cross_pts[i + 1]
        canvas.create_line(x_start, y_start,
                           x_end,   y_end,
                           fill=col_start, width=2)

def _show_single_team_form_graph(team: str,
                                 form_timeline,
                                 all_dates,
                                 current_form: Optional[Tuple[str, float, str]] = None):
    """
    Create a Tkinter window that shows a single team’s FORM over time.
    The line is *tier‑aware*: whenever the line crosses a tier boundary
    (S / A / B / C / D) it is automatically split and drawn with the
    colour that belongs to each tier.

    Parameters
    ----------
    team            – team name (string)
    form_timeline   – list of (date_str, score, grade, match_index)
    all_dates       – sorted list of every distinct date that appears in
                      the timeline (strings “YYYY‑MM‑DD”)
    current_form    – optional (grade, score, streak) tuple for the
                      most recent form; if supplied it will be drawn as
                      a horizontal “current” marker at the right edge.
    """
    import tkinter as tk
    from datetime import datetime, timedelta

    # ------------------------------------------------------------------
    #   Geometry helpers (same as the original function)
    # ------------------------------------------------------------------
    date_objects = [datetime.strptime(d, "%Y-%m-%d") for d in all_dates]

    # Add a one‑day buffer on the left so the graph never starts
    # exactly at the first point (gives a little breathing room).
    first_date = date_objects[0]
    buffer_start = first_date - timedelta(days=1)

    all_dates_with_buffer = [buffer_start.strftime("%Y-%m-%d")] + all_dates
    date_objects_with_buffer = [buffer_start] + date_objects

    total_days = (date_objects_with_buffer[-1] -
                  date_objects_with_buffer[0]).days + 1

    # Fixed Y‑range for form (0‑100) – a little vertical padding is added.
    f_min, f_max = 0, 100
    f_range = f_max - f_min

    # ------------------------------------------------------------------
    #   Tier colours and the thresholds that separate them
    # ------------------------------------------------------------------
    tier_colors = {
        'S': '#FFD700',   # gold
        'A': '#C0C0C0',   # silver
        'B': '#CD7F32',   # bronze
        'C': '#3cb44b',   # green
        'D': '#e6194B',   # red
    }

    # Thresholds in **descending** order – they correspond to the
    # horizontal grid lines that appear on the left side of the graph.
    thresholds = [100, 85, 70, 55, 40, 0]

    # ------------------------------------------------------------------
    #   Helper that draws a (possibly split) segment respecting the tiers
    # ------------------------------------------------------------------
    def _draw_form_segment(canvas,
                           x1, y1, s1,          # start pixel + score
                           x2, y2, s2,          # end   pixel + score
                           tier_colors,
                           thresholds,
                           m_top, g_height,
                           f_min, f_range):
        """
        Draw a line between (x1,y1)→(x2,y2) where the underlying scores
        are s1→s2.  If the segment crosses any tier boundary it is split
        into sub‑segments, each coloured according to the tier it belongs
        to.
        """
        # --------------------------------------------------------------
        #   Determine which tier a raw score belongs to
        # --------------------------------------------------------------
        def tier_of(score):
            if score >= 85:   return 'S'
            if score >= 70:   return 'A'
            if score >= 55:   return 'B'
            if score >= 40:   return 'C'
            return 'D'

        col_start = tier_colors[tier_of(s1)]
        col_end   = tier_colors[tier_of(s2)]

        # Same colour → simple line, nothing to split.
        if col_start == col_end:
            canvas.create_line(x1, y1, x2, y2, fill=col_start, width=2)
            return

        # ----------------------------------------------------------------
        #   The segment crosses at least one threshold.  Build a list of
        #   all crossing points (including the original start/end).
        # ----------------------------------------------------------------
        # We always store (x, y, score, colour) for each point.
        cross_pts = [(x1, y1, s1, col_start)]

        # Work from the higher score towards the lower score.
        lo, hi = (s2, s1) if s1 > s2 else (s1, s2)   # lo < hi
        ascending = s2 > s1

        for thr in thresholds:
            if lo < thr <= hi:                # the line hits this boundary
                # proportion along the line where the threshold is hit
                if s1 > s2:    # descending
                    t = (s1 - thr) / (s1 - s2)
                else:          # ascending
                    t = (thr - s1) / (s2 - s1)

                xc = x1 + t * (x2 - x1)
                yc = m_top + g_height - ((thr - f_min) / f_range * g_height)

                # colour AFTER crossing: above threshold when ascending,
                # below threshold when descending
                if ascending:
                    col_after = tier_colors[tier_of(thr + 0.01)]
                else:
                    col_after = tier_colors[tier_of(thr - 0.01)]
                cross_pts.append((xc, yc, thr, col_after))

        cross_pts.append((x2, y2, s2, col_end))

        # Sort crossing points by x so ascending and descending both draw correctly
        cross_pts.sort(key=lambda p: p[0])

        # ----------------------------------------------------------------
        #   Draw each sub‑segment with its own colour.
        # ----------------------------------------------------------------
        for i in range(len(cross_pts) - 1):
            x_s, y_s, _, col_s = cross_pts[i]
            x_e, y_e, _, _       = cross_pts[i + 1]
            canvas.create_line(x_s, y_s, x_e, y_e,
                               fill=col_s, width=2)

    # ------------------------------------------------------------------
    #   Build the Tkinter window
    # ------------------------------------------------------------------
    root = tk.Tk()
    root.title(f"{team} – Form Over Time")
    root.configure(bg="#1a1a1a")

    width, height = 1500, 600
    root.geometry(f"{width}x{height}")

    canvas = tk.Canvas(root, bg="#1a1a1a", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    close_btn = [None]               # mutable holder for the Close button

    # ------------------------------------------------------------------
    #   Redraw on resize (keeps everything proportional)
    # ------------------------------------------------------------------
    def on_resize(event):
        canvas.configure(scrollregion=(0, 0, event.width, event.height))
        draw_graph(event.width, event.height)
        if close_btn[0]:
            close_btn[0].place(x=event.width // 2 - 40,
                               y=event.height - 45)

    canvas.bind("<Configure>", on_resize)

    # ------------------------------------------------------------------
    #   Main drawing routine
    # ------------------------------------------------------------------
    def draw_graph(cw, ch):
        canvas.delete("all")

        # ---- margins -------------------------------------------------
        m_left, m_top, m_bottom = 80, 60, 100
        m_right = 100
        g_width = int((cw - m_left - m_right) * 0.95)
        g_right = m_left + g_width
        g_height = max(100, ch - m_top - m_bottom)

        # pixels per day for the horizontal axis
        px_day = g_width / total_days if total_days > 1 else g_width

        # ---- title --------------------------------------------------
        canvas.create_text(cw // 2, 25,
                           text=f"{team} – Form Over Time",
                           font=("Arial", 14, "bold"),
                           fill="white")

        # ---- subtitle (current form + date range) ------------------
        today_str = datetime.now().strftime("%Y-%m-%d")
        last_form_date = form_timeline[-1][0] if form_timeline else "?"
        days_since = (datetime.strptime(today_str, "%Y-%m-%d") -
                      datetime.strptime(last_form_date, "%Y-%m-%d")).days
        inactive_str = f"  |  Last match: {days_since}d ago" if days_since > 0 else ""
        if current_form:
            grade, score, _ = current_form
            sub = (f"Current: {grade} ({int(score)}/100){inactive_str}  |  "
                   f"Range: {all_dates_with_buffer[1]} to {today_str}")
        else:
            sub = f"Current: –{inactive_str}  |  Range: {all_dates_with_buffer[1]} to {today_str}"
        canvas.create_text(cw // 2, 45,
                           text=sub,
                           font=("Arial", 9),
                           fill="#888888")

        # ---- date → x map -----------------------------------------
        date_to_x = {}
        start_dt = date_objects_with_buffer[0]
        for i, dt in enumerate(date_objects_with_buffer):
            days_off = (dt - start_dt).days
            date_to_x[all_dates_with_buffer[i]] = m_left + days_off * px_day

        # ---- horizontal background grid (neutral, like rating graph) ---
        for i in range(6):
            y_val = f_min + i * (f_range / 5)
            y_pos = m_top + g_height - (i / 5 * g_height)
            canvas.create_line(m_left, y_pos, g_right, y_pos,
                               fill='#2a2a2a', dash=(5, 5))

        # ---- tier threshold lines (coloured, on top of grid) -----------
        tier_label_map = {100: 'S', 85: 'A', 70: 'B', 55: 'C', 40: 'D', 0: ''}
        for thr in thresholds:
            if thr == 0:
                continue   # skip 0 — already covered by x-axis
            y = m_top + g_height - ((thr - f_min) / f_range * g_height)
            col = tier_colors[
                'S' if thr >= 85 else
                'A' if thr >= 70 else
                'B' if thr >= 55 else
                'C' if thr >= 40 else
                'D'
            ]
            canvas.create_line(m_left, y, g_right, y,
                               fill=col, dash=(6, 3), width=1)
            canvas.create_text(m_left - 8, y,
                               text=str(thr),
                               font=('Arial', 8, 'bold'),
                               fill=col, anchor='e')
            # tier letter label on the right
            canvas.create_text(g_right + 8, y,
                               text=tier_label_map.get(thr, ''),
                               font=('Arial', 8, 'bold'),
                               fill=col, anchor='w')

        # ---- axes --------------------------------------------------
        canvas.create_line(m_left, m_top,
                           m_left, m_top + g_height,
                           fill='white', width=1)
        canvas.create_line(m_left, m_top + g_height,
                           g_right, m_top + g_height,
                           fill='white', width=1)

        # ---- current-form horizontal reference line -----------------
        if current_form:
            grade, score, streak = current_form
            cur_y = m_top + g_height - ((score - f_min) / f_range * g_height)
            cur_col = tier_colors[
                'S' if score >= 85 else
                'A' if score >= 70 else
                'B' if score >= 55 else
                'C' if score >= 40 else
                'D'
            ]
            canvas.create_line(m_left, cur_y, g_right, cur_y,
                               fill=cur_col, dash=(8, 4), width=1)
            canvas.create_text(g_right + 8, cur_y,
                               text=f"Current: {int(score)}",
                               font=('Arial', 8, 'bold'),
                               fill=cur_col, anchor='w')

        # ----------------------------------------------------------------
        #   Convert the raw form timeline into canvas points.
        #   Each entry is (date_str, score, grade, match_idx)
        # ----------------------------------------------------------------
        points = []
        for d, score, grade, idx in form_timeline:
            if d not in date_to_x:
                continue
            x = date_to_x[d]
            y = m_top + g_height - ((score - f_min) / f_range * g_height)
            points.append((x, y, d, score, grade))

        # Keep only the *latest* entry for a given date (in case of duplicates)
        points.sort(key=lambda p: p[0])
        uniq = {}
        for p in points:
            uniq[p[2]] = p                # later point overwrites earlier
        points = sorted(uniq.values(), key=lambda p: p[0])

        # ----------------------------------------------------------------
        #   Draw the (tier‑aware) form line
        # ----------------------------------------------------------------
        for i in range(len(points) - 1):
            x1, y1, d1, s1, g1 = points[i]
            x2, y2, d2, s2, g2 = points[i + 1]

            _draw_form_segment(canvas,
                               x1, y1, s1,
                               x2, y2, s2,
                               tier_colors,
                               thresholds,
                               m_top, g_height,
                               f_min, f_range)

        # ----------------------------------------------------------------
        #   Draw markers for every actual point (match dates)
        # ----------------------------------------------------------------
        for x, y, d, score, grade in points:
            col = tier_colors[
                'S' if score >= 85 else
                'A' if score >= 70 else
                'B' if score >= 55 else
                'C' if score >= 40 else
                'D'
            ]
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                               fill=col,
                               outline="white",
                               width=2)

        # ---- current-form reference line label fix --------------------
        # (label already drawn above in grid section — just fix text)

        # ----------------------------------------------------------------
        #   Solid trailing line from last match point → g_right
        #   (form hasn't changed; solid not dashed to distinguish from
        #    the tier reference lines which are dashed)
        # ----------------------------------------------------------------
        if points:
            last_x, last_y, _, last_score, _ = points[-1]
            if g_right > last_x:
                last_col = tier_colors[
                    'S' if last_score >= 85 else
                    'A' if last_score >= 70 else
                    'B' if last_score >= 55 else
                    'C' if last_score >= 40 else
                    'D'
                ]
                canvas.create_line(last_x, last_y, g_right, last_y,
                                   fill=last_col, width=2)
                canvas.create_oval(g_right - 5, last_y - 5,
                                   g_right + 5, last_y + 5,
                                   fill='#1a1a1a',
                                   outline=last_col, width=2)

        # ----------------------------------------------------------------
        #   Date‑axis labels (spaced out to avoid crowding)
        # ----------------------------------------------------------------
        min_spacing = 100
        shown = []
        sorted_pos = sorted(date_to_x.items(), key=lambda kv: kv[1])
        if sorted_pos:
            last_x = sorted_pos[-1][1] + min_spacing
            for d, x in reversed(sorted_pos):
                if last_x - x >= min_spacing:
                    shown.append((x, d))
                    last_x = x
            shown.reverse()
            if len(shown) < 2 and len(sorted_pos) >= 2:
                shown = [sorted_pos[-2], sorted_pos[-1]]

        base_y = m_top + g_height
        for x, d in shown:
            canvas.create_text(x, base_y + 12,
                               text='|', font=('Arial', 8), fill='#888888')
            canvas.create_text(x, base_y + 28,
                               text=d, font=('Arial', 7),
                               fill='#888888', anchor='n')

        # ----------------------------------------------------------------
        #   Legend
        # ----------------------------------------------------------------
        leg_x = m_left
        leg_y = m_top + g_height + 50

        for i, (letter, col, rng) in enumerate([
            ('S', tier_colors['S'], '85–100'),
            ('A', tier_colors['A'], '70–84'),
            ('B', tier_colors['B'], '55–69'),
            ('C', tier_colors['C'], '40–54'),
            ('D', tier_colors['D'], '0–39'),
        ]):
            cx = leg_x + i * 110
            canvas.create_oval(cx, leg_y, cx + 10, leg_y + 10,
                               fill=col, outline='white', width=1)
            canvas.create_text(cx + 16, leg_y + 5,
                               text=f"{letter}  {rng}",
                               font=('Arial', 8), fill='#888888', anchor='w')

        # Current-form note
        if current_form:
            grade, score, streak = current_form
            streak_str = f"  |  Streak: {streak}" if streak else ""
            canvas.create_text(leg_x, leg_y + 22,
                               text=f"Current form: {grade}  ({int(score)}/100){streak_str}",
                               font=('Arial', 8), fill='#aaaaaa', anchor='w')

        # ----------------------------------------------------------------
        #   Close button
        # ----------------------------------------------------------------
        if close_btn[0] is None:
            close_btn[0] = tk.Button(root,
                                    text="Close",
                                    command=root.destroy,
                                    bg="#404040",
                                    fg="white",
                                    font=("Arial", 10),
                                    padx=30,
                                    pady=5)
        close_btn[0].place(x=cw // 2 - 40, y=ch - 45)

    # ------------------------------------------------------------------
    #   Initial draw
    # ------------------------------------------------------------------
    draw_graph(width, height)
    root.mainloop()

def _show_combined_graph(team,
                         rating_timeline,
                         form_timeline,
                         all_dates,
                         current_rating,
                         peak_rating,
                         peak_date,
                         current_form: Optional[Tuple[str, float, str]] = None):
    """
    Plot a team’s *rating* (with depreciation) **and** its *form* on the
    same graph.  The rating line is drawn exactly as before.
    The form line uses the same tier‑aware colour‑splitting logic as the
    solo‑form graph.

    Parameters
    ----------
    team                – team name (string)
    rating_timeline     – list of (date_str, rating, type) where type is
                          'start', 'match', 'depreciated', or 'current'
    form_timeline       – list of (date_str, score, grade, match_idx)
    all_dates           – sorted list of every distinct date that appears
                          in either timeline
    current_rating      – the *non‑depreciated* rating (used to compute
                          the depreciation line)
    peak_rating         – highest rating the team ever achieved
    peak_date           – date of that peak rating
    current_form        – optional (grade, score, streak) for the most
                          recent form; drawn as a horizontal marker.
    """
    import tkinter as tk
    from datetime import datetime, timedelta

    # ------------------------------------------------------------------
    #   Geometry helpers (same as in the original combined graph)
    # ------------------------------------------------------------------
    date_objects = [datetime.strptime(d, "%Y-%m-%d") for d in all_dates]

    first_date = date_objects[0]
    buffer_start = first_date - timedelta(days=1)

    all_dates_with_buffer = [buffer_start.strftime("%Y-%m-%d")] + all_dates
    date_objects_with_buffer = [buffer_start] + date_objects

    total_days = (date_objects_with_buffer[-1] -
                  date_objects_with_buffer[0]).days + 1

    # Rating Y‑range (a little padding)
    rating_vals = [r for _, r, _, _ in rating_timeline if r is not None]
    r_min = min(rating_vals)
    r_max = max(rating_vals)
    pad = (r_max - r_min) * 0.1 if r_max != r_min else 50
    rating_min = max(0, r_min - pad)
    rating_max = min(RATING_CAP, r_max + pad)
    rating_min = (int(rating_min) // 50) * 50
    rating_max = ((int(rating_max) // 50) + 1) * 50

    # Form Y‑range (fixed 0‑100)
    f_min, f_max = 0, 100
    f_range = f_max - f_min

    # ------------------------------------------------------------------
    #   Compute depreciation for "today" (mirrors _show_single_team_graph)
    # ------------------------------------------------------------------
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_match_date = None
    for d, _, pt_type, _ in rating_timeline:
        if pt_type == 'match':
            last_match_date = d

    depreciated = current_rating
    days_inactive = 0
    if last_match_date:
        last_dt = datetime.strptime(last_match_date, "%Y-%m-%d")
        today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        days_inactive = (today_dt - last_dt).days
        if days_inactive > DEPRECIATION_THRESHOLD:
            depreciated = calculate_depreciation(
                current_rating, days_inactive, team
            )

    graph_data = {
        'current_rating':     current_rating,
        'depreciated_rating': depreciated,
        'days_inactive':      days_inactive,
        'peak_rating':        peak_rating,
        'all_dates':          all_dates_with_buffer,
    }

    # ------------------------------------------------------------------
    #   Tier colours / thresholds – reused for the form line
    # ------------------------------------------------------------------
    tier_colors = {
        'S': '#FFD700',   # gold
        'A': '#C0C0C0',   # silver
        'B': '#CD7F32',   # bronze
        'C': '#3cb44b',   # green
        'D': '#e6194B',   # red
    }
    thresholds = [100, 85, 70, 55, 40, 0]

    # ------------------------------------------------------------------
    #   Helper that draws a tier‑aware form segment (exactly the same as
    #   the one used in the solo‑form graph)
    # ------------------------------------------------------------------
    def _draw_form_segment(canvas,
                           x1, y1, s1,
                           x2, y2, s2,
                           tier_colors,
                           thresholds,
                           m_top, g_height,
                           f_min, f_range):
        def tier_of(score):
            if score >= 85:   return 'S'
            if score >= 70:   return 'A'
            if score >= 55:   return 'B'
            if score >= 40:   return 'C'
            return 'D'

        col_start = tier_colors[tier_of(s1)]
        col_end   = tier_colors[tier_of(s2)]

        if col_start == col_end:
            canvas.create_line(x1, y1, x2, y2, fill=col_start, width=2)
            return

        cross_pts = [(x1, y1, s1, col_start)]
        lo, hi = (s2, s1) if s1 > s2 else (s1, s2)

        for thr in thresholds:
            if lo < thr <= hi:
                if s1 > s2:
                    t = (s1 - thr) / (s1 - s2)
                else:
                    t = (thr - s1) / (s2 - s1)
                xc = x1 + t * (x2 - x1)
                yc = m_top + g_height - ((thr - f_min) / f_range * g_height)
                col_after = tier_colors[tier_of(thr - 0.01)]
                cross_pts.append((xc, yc, thr, col_after))

        cross_pts.append((x2, y2, s2, col_end))

        for i in range(len(cross_pts) - 1):
            xs, ys, _, cs = cross_pts[i]
            xe, ye, _, _   = cross_pts[i + 1]
            canvas.create_line(xs, ys, xe, ye, fill=cs, width=2)

    # ------------------------------------------------------------------
    #   Build the window
    # ------------------------------------------------------------------
    #   Window geometry & colour
    # ------------------------------------------------------------------
    team_color = '#3cb44b'      # default line colour
    width, height = 1600, 600

    root = tk.Tk()
    root.title(f"{team} – Rating & Form Over Time")
    root.geometry(f"{width}x{height}")
    root.configure(bg="#1a1a1a")

    canvas = tk.Canvas(root, bg="#1a1a1a", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    close_btn = [None]

    # ------------------------------------------------------------------
    #   Resize handling
    # ------------------------------------------------------------------
    def on_resize(event):
        canvas.configure(scrollregion=(0, 0, event.width, event.height))
        draw_graph(event.width, event.height)
        if close_btn[0]:
            close_btn[0].place(x=event.width // 2 - 40,
                               y=event.height - 45)

    canvas.bind("<Configure>", on_resize)

    # ------------------------------------------------------------------
    #   Main drawing routine
    # ------------------------------------------------------------------
    def draw_graph(cw, ch):
        canvas.delete("all")

        # ---- margins -------------------------------------------------
        m_left, m_top, m_bottom = 80, 60, 130
        m_right = 150
        g_width = int((cw - m_left - m_right) * 0.95)
        g_right = m_left + g_width
        g_height = max(100, ch - m_top - m_bottom)

        px_day = g_width / total_days if total_days > 1 else g_width

        # ---- title --------------------------------------------------
        canvas.create_text(cw // 2, 25,
                           text=f"{team} – Rating & Form Over Time",
                           font=("Arial", 14, "bold"),
                           fill="white")

        # ---- subtitle (rating + form) -------------------------------
        dep_info = ""
        if graph_data['days_inactive'] > DEPRECIATION_THRESHOLD:
            loss = int(graph_data['current_rating'] -
                       graph_data['depreciated_rating'])
            dep_info = f" | Dep: -{loss} ({graph_data['days_inactive']}d)"
        rating_part = f"Rating: {int(graph_data['current_rating'])}{dep_info}"
        form_part   = (f"Form: {current_form[0]} {int(current_form[1])}"
                       if current_form else "Form: –")
        stats = f"{rating_part}  |  {form_part}  |  " \
                f"Range: {graph_data['all_dates'][1]} to {graph_data['all_dates'][-1]}"
        canvas.create_text(cw // 2, 45,
                           text=stats,
                           font=("Arial", 9),
                           fill="#888888")

        # ---- date → x map -----------------------------------------
        date_to_x = {}
        start_dt = date_objects_with_buffer[0]
        for i, dt in enumerate(date_objects_with_buffer):
            days_off = (dt - start_dt).days
            date_to_x[all_dates_with_buffer[i]] = m_left + days_off * px_day

        end_x = date_to_x.get(all_dates_with_buffer[-1], g_right)

        # ---- rating grid lines with styled labels ------------------
        for i in range(6):
            y_val = rating_min + i * (rating_max - rating_min) / 5
            y_pos = m_top + g_height - (i / 5 * g_height)
            canvas.create_line(m_left, y_pos, end_x, y_pos,
                               fill="#2a2a2a", dash=(5, 5))
            canvas.create_text(m_left - 8, y_pos,
                               text=str(int(y_val)),
                               font=("Arial", 8, "bold"), fill="#aaaaaa",
                               anchor="e")

        # ---- elite threshold reference line -------------------------
        if rating_min <= ELITE_THRESHOLD <= rating_max:
            elite_y = m_top + g_height - (
                (ELITE_THRESHOLD - rating_min) /
                (rating_max - rating_min) * g_height
            )
            canvas.create_line(m_left, elite_y, end_x, elite_y,
                               fill="#4363d8", dash=(6, 3), width=1)
            canvas.create_text(m_left - 8, elite_y,
                               text=str(int(ELITE_THRESHOLD)),
                               font=("Arial", 8, "bold"), fill="#4363d8",
                               anchor="e")
            canvas.create_text(end_x + 8, elite_y,
                               text="Elite",
                               font=("Arial", 8, "bold"), fill="#4363d8",
                               anchor="w")

        # ---- form horizontal tier lines (left side) ---------------
        for thr in thresholds:
            y = m_top + g_height - ((thr - f_min) / f_range * g_height)
            col = tier_colors[
                'S' if thr == 100 else
                'A' if thr == 85 else
                'B' if thr == 70 else
                'C' if thr == 55 else
                'D'
            ]
            canvas.create_line(m_left, y, end_x, y,
                               fill=col, dash=(4, 4), width=1)
            canvas.create_text(m_left - 10, y,
                               text=str(thr),
                               font=("Arial", 8, "bold"),
                               fill=col,
                               anchor="e")

        # ---- axes --------------------------------------------------
        canvas.create_line(m_left, m_top,
                           m_left, m_top + g_height,
                           fill="white", width=1)
        canvas.create_line(m_left, m_top + g_height,
                           end_x, m_top + g_height,
                           fill="white", width=1)

        # ---- peak‑rating line ---------------------------------------
        peak_y = m_top + g_height - (
            (graph_data['peak_rating'] - rating_min) /
            (rating_max - rating_min) * g_height
        )
        canvas.create_line(m_left, peak_y, end_x, peak_y,
                           fill="#FFD700", dash=(8, 4), width=1)
        canvas.create_text(end_x + 12, peak_y,
                           text=f"Peak: {int(graph_data['peak_rating'])}",
                           font=("Arial", 8, "bold"), fill="#FFD700",
                           anchor="w")

        # ---- current‑rating line (depreciated) --------------------
        cur_y = m_top + g_height - (
            (graph_data['depreciated_rating'] - rating_min) /
            (rating_max - rating_min) * g_height
        )
        canvas.create_line(m_left, cur_y, end_x, cur_y,
                           fill=team_color, dash=(8, 4), width=1)
        canvas.create_text(end_x + 12, cur_y,
                           text=f"Current: {int(graph_data['depreciated_rating'])}",
                           font=("Arial", 8, "bold"), fill=team_color,
                           anchor="w")

        # ----------------------------------------------------------------
        # ----------------------------------------------------------------
        #   Convert rating timeline → canvas points
        # ----------------------------------------------------------------
        rating_pts = []
        for d, rating, ptype, pts_before in rating_timeline:
            if d in date_to_x and rating is not None and ptype != 'start':
                x = date_to_x[d]
                y = m_top + g_height - (
                    (rating - rating_min) / (rating_max - rating_min) * g_height
                )
                rating_pts.append((x, y, d, rating, ptype, pts_before))
        rating_pts.sort(key=lambda p: p[0])

        # Final inactive period → depreciated marker
        if graph_data['days_inactive'] > DEPRECIATION_THRESHOLD and rating_pts:
            x_last, _, d_last, _, _, pb = rating_pts[-1]
            r_range_val = rating_max - rating_min
            y_dep = m_top + g_height - (
                (graph_data['depreciated_rating'] - rating_min) / r_range_val * g_height
            )
            rating_pts[-1] = (x_last, y_dep, d_last,
                              graph_data['depreciated_rating'], 'depreciated', pb)

        # arrival_y[i] = pts_before Y; departure_y/r[i] = pts_after Y/rating
        r_range_val = rating_max - rating_min
        arrival_y   = {}
        departure_y = {}
        departure_r = {}
        for i, (x, y, d, rating, ptype, pts_before) in enumerate(rating_pts):
            departure_y[i] = y
            departure_r[i] = rating
            if pts_before is not None and ptype == 'match':
                arr_y = m_top + g_height - ((pts_before - rating_min) / r_range_val * g_height)
                arrival_y[i] = arr_y
            else:
                arrival_y[i] = y

        # ---- start-rating line/marker -----------------------------------
        start_rating_entry = next(
            (r for _, r, pt, _ in rating_timeline if pt == 'start'), None
        )
        start_rating = start_rating_entry if start_rating_entry is not None \
                       else STARTING_TEAMS.get(team, 1000)
        start_x = m_left
        start_y = m_top + g_height - (
            (start_rating - rating_min) / r_range_val * g_height
        )
        if rating_pts:
            canvas.create_line(start_x, start_y,
                               rating_pts[0][0], arrival_y[0],
                               fill=team_color, width=2)

        # ---- draw rating lines (including depreciation curves) ----------
        for i in range(len(rating_pts) - 1):
            x1 = rating_pts[i][0];   d1 = rating_pts[i][2]
            x2 = rating_pts[i+1][0]; d2 = rating_pts[i+1][2]
            _draw_depreciation_curve(
                canvas, x1, departure_y[i], departure_r[i],
                x2, arrival_y[i+1], departure_r[i+1],
                team_color, m_top, g_height,
                rating_min, r_range_val, d1, d2, team
            )

        # ---- rating markers (dots at arrival_y) ----
        for i, (x, _, d, rating, ptype, _pb) in enumerate(rating_pts):
            y = arrival_y[i]
            if ptype in ('depreciated', 'current'):
                canvas.create_oval(x-5, y-5, x+5, y+5,
                                   fill=team_color, outline="white", width=2)
            elif rating == graph_data['peak_rating'] and ptype not in ('depreciated', 'current'):
                py = departure_y[i]
                canvas.create_oval(x-5, py-5, x+5, py+5,
                                   fill="#1a1a1a", outline="#FFD700", width=2)
            elif ptype == 'match':
                canvas.create_oval(x-3, y-3, x+3, y+3,
                                   fill=team_color, outline="white", width=1)

        # ---- start-rating marker ----------------------------------------
        canvas.create_oval(start_x-5, start_y-5, start_x+5, start_y+5,
                           fill="#1a1a1a", outline="#4363d8", width=2)

        # ----------------------------------------------------------------
        form_pts = []
        for d, score, grade, _ in form_timeline:
            if d not in date_to_x:
                continue
            x = date_to_x[d]
            y = m_top + g_height - ((score - f_min) / f_range * g_height)
            form_pts.append((x, y, d, score, grade))

        form_pts.sort(key=lambda p: p[0])

        # --------------------------------------------------------------
        #   Draw the tier‑aware form line (uses the helper defined above)
        # --------------------------------------------------------------
        for i in range(len(form_pts) - 1):
            x1, y1, d1, s1, g1 = form_pts[i]
            x2, y2, d2, s2, g2 = form_pts[i + 1]

            _draw_form_segment(canvas,
                               x1, y1, s1,
                               x2, y2, s2,
                               tier_colors,
                               thresholds,
                               m_top, g_height,
                               f_min, f_range)

        # ---- form point markers ------------------------------------
        for x, y, d, score, grade in form_pts:
            col = tier_colors[
                'S' if score >= 85 else
                'A' if score >= 70 else
                'B' if score >= 55 else
                'C' if score >= 40 else
                'D'
            ]
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                               fill=col,
                               outline="white",
                               width=2)

        # ---- current‑form horizontal line (if supplied) ----------
        if current_form:
            grade, score, streak = current_form
            cur_y = m_top + g_height - ((score - f_min) / f_range * g_height)
            canvas.create_line(m_left, cur_y, end_x, cur_y,
                               fill="#FFFFFF", dash=(4, 4), width=1)
            canvas.create_oval(end_x - 5, cur_y - 5,
                               end_x + 5, cur_y + 5,
                               fill=tier_colors[
                                   'S' if score >= 85 else
                                   'A' if score >= 70 else
                                   'B' if score >= 55 else
                                   'C' if score >= 40 else
                                   'D'
                               ],
                               outline="white",
                               width=2)

        # ----------------------------------------------------------------
        #   Date‑axis labels (spaced out)
        # ----------------------------------------------------------------
        min_spacing = 100
        shown = []
        sorted_pos = sorted(date_to_x.items(), key=lambda kv: kv[1])
        if sorted_pos:
            last_x = sorted_pos[-1][1] + min_spacing
            for d, x in reversed(sorted_pos):
                if last_x - x >= min_spacing:
                    shown.append((x, d))
                    last_x = x
            shown.reverse()
            if len(shown) < 2 and len(sorted_pos) >= 2:
                shown = [sorted_pos[-2], sorted_pos[-1]]

        base_y = m_top + g_height
        for x, d in shown:
            canvas.create_text(x, base_y + 12,
                               text="|", font=("Arial", 8),
                               fill="#888888")
            canvas.create_text(x, base_y + 28,
                               text=d, font=("Arial", 7),
                               fill="#888888", anchor="n")

        # ----------------------------------------------------------------
        #   Legends (rating + form tiers)
        # ----------------------------------------------------------------
        leg_x = m_left
        leg_y = m_top + g_height + 45

        # Rating legend (peak & current)
        canvas.create_oval(leg_x, leg_y,
                           leg_x + 10, leg_y + 10,
                           fill="#1a1a1a", outline="#FFD700", width=2)
        canvas.create_text(leg_x + 20, leg_y + 5,
                           text="Peak Rating",
                           font=("Arial", 8), fill="#888888",
                           anchor="w")
        canvas.create_oval(leg_x + 120, leg_y,
                           leg_x + 130, leg_y + 10,
                           fill=team_color, outline="white", width=2)
        canvas.create_text(leg_x + 140, leg_y + 5,
                           text="Current Rating",
                           font=("Arial", 8), fill="#888888",
                           anchor="w")

        # Form tier legend (right side)
        for i, (tier, col) in enumerate([
            ("S", tier_colors['S']),
            ("A", tier_colors['A']),
            ("B", tier_colors['B']),
            ("C", tier_colors['C']),
            ("D", tier_colors['D']),
        ]):
            cx = leg_x + i * 80
            canvas.create_oval(cx, leg_y + 30,
                               cx + 12, leg_y + 42,
                               fill=col, outline="white", width=1)
            canvas.create_text(cx + 18, leg_y + 36,
                               text=tier,
                               font=("Arial", 8),
                               fill="#888888",
                               anchor="w")

        # ----------------------------------------------------------------
        #   Close button
        # ----------------------------------------------------------------
        if close_btn[0] is None:
            close_btn[0] = tk.Button(root,
                                    text="Close",
                                    command=root.destroy,
                                    bg="#404040",
                                    fg="white",
                                    font=("Arial", 10),
                                    padx=30,
                                    pady=5)
        close_btn[0].place(x=cw // 2 - 40, y=ch - 45)

    # ------------------------------------------------------------------
    #   Initial draw
    # ------------------------------------------------------------------
    draw_graph(width, height)
    root.mainloop()

# =============================================================================
# === ANALYTICS TOOLS ===
# =============================================================================

def team_analytics_menu() -> None:
    """Submenu for team-specific analytics and visualizations."""
    options = [
        ('1', 'Team Rating Graph', view_team_rating_graph),
        ('2', 'Team Form Graph', view_team_form_graph),
        ('3', 'Combined Rating + Form Graph', view_team_combined_graph),
        ('4', 'Elite Teams Over Time', display_elite_teams_over_time),
        ('5', 'Map Score Distribution', display_map_score_distribution),
        ('6', 'Form Table (All Teams)', display_form_table),
        ('7', 'Analyze Team Form', analyze_team_form),
        ('8', 'Compare Teams', compare_teams),
        ('0', 'Back', None),
    ]
    
    while True:
        print_menu(
            "TEAM ANALYTICS",
            [
                ("1", "Rating Graph"),
                ("2", "Form Graph"),
                ("3", "Combined Rating + Form Graph"),
                ("4", "Elite Teams Over Time"),
                (None, None),
                ("5", "Map Score Distribution"),
                ("6", "Form Table  (All Teams)"),
                ("7", "Analyse Team Form"),
                ("8", "Compare Teams"),
                (None, None),
                ("0", "Back"),
            ],
            subtitle="Graphs, Form & Comparisons",
        )
        
        choice = get_cmd(check_cmd(input("Select: ")))
        
        if choice in ['0', 'back']:
            break
        
        found = False
        for num, _, func in options:
            if choice == num:
                func()
                found = True
                break
        if not found:
            print_warning("Invalid choice. Try again.")

def win_probability_calculator() -> None:
    """Calculate win probabilities for a match."""
    print("\nWIN PROBABILITY CALCULATOR")
    print("=" * 50)

    t1_raw = check_cmd(input("Enter Team 1: ")).strip()
    if get_cmd(t1_raw) in ['back', '0']:
        return
    t1 = find_team(t1_raw)
    if not t1:
        print("[!] Team not found.")
        return

    t2_raw = check_cmd(input("Enter Team 2: ")).strip()
    if get_cmd(t2_raw) in ['back', '0']:
        return
    t2 = find_team(t2_raw)
    if not t2:
        print("[!] Team not found.")
        return
    
    if t1 == t2:
        print("[!] Cannot calculate probability for the same team.")
        return

    p1 = teams[t1]
    p2 = teams[t2]

    form1 = calculate_form(t1, n=15, history=history)
    form2 = calculate_form(t2, n=15, history=history)
    p1_eff = p1
    p2_eff = p2
    if form1 and form2:
        ans = get_cmd(check_cmd(input("Use form adjustments? (y/n): ")))
        if ans == 'y':
            p1_eff = p1 + (form1[1] - 50)
            p2_eff = p2 + (form2[1] - 50)
    else:
        if form1 is None or form2 is None:
            print("(Form data not available for both teams; using raw ratings)")

    p_map = 1 / (1 + 10 ** ((p2_eff - p1_eff) / 400))

    print_menu(
        "SELECT MATCH FORMAT",
        [
            ("1", "Best-of-3"),
            ("2", "Best-of-5  (Grand Final)"),
            (None, None),
            ("0", "Back"),
        ],
    )
    fmt_choice = get_cmd(check_cmd(input("Select: ")))
    if fmt_choice in ['back', '0']:
        return
    if fmt_choice not in ['1', '2']:
        fmt_choice = '1'

    if fmt_choice == '1':
        best_of = 3
        p_win = p_map**2 + 2 * p_map**2 * (1 - p_map)
        p_lose = 1 - p_win
        p_win_2_0 = p_map**2
        p_win_2_1 = 2 * p_map**2 * (1 - p_map)
        p_lose_0_2 = (1 - p_map)**2
        p_lose_1_2 = 2 * p_map * (1 - p_map)**2
    else:
        best_of = 5
        p_win = (p_map**3 +
                 3 * p_map**3 * (1 - p_map) +
                 6 * p_map**3 * ((1 - p_map)**2))
        p_lose = 1 - p_win
        p_win_3_0 = p_map**3
        p_win_3_1 = 3 * p_map**3 * (1 - p_map)
        p_win_3_2 = 6 * p_map**3 * ((1 - p_map)**2)
        p_lose_0_3 = (1 - p_map)**3
        p_lose_1_3 = 3 * p_map * ((1 - p_map)**3)
        p_lose_2_3 = 6 * (p_map**2) * ((1 - p_map)**3)

    print("\n" + "=" * 50)
    print(f"MATCH WIN PROBABILITY (Best-of-{best_of})")
    print("-" * 50)
    print(f"  {t1}: {p_win*100:5.1f}%")
    print(f"  {t2}: {p_lose*100:5.1f}%")
    print("-" * 50)

    show_detail = get_cmd(check_cmd(input("\nShow map score breakdown? (y/n): ")))
    if show_detail != 'y':
        return

    print("\nMap Score Probabilities:")
    if best_of == 3:
        print(f"  {t1} 2-0 : {p_win_2_0*100:5.1f}%")
        print(f"  {t1} 2-1 : {p_win_2_1*100:5.1f}%")
        print(f"  {t2} 2-0 : {p_lose_0_2*100:5.1f}%")
        print(f"  {t2} 2-1 : {p_lose_1_2*100:5.1f}%")
    else:
        print(f"  {t1} 3-0 : {p_win_3_0*100:5.1f}%")
        print(f"  {t1} 3-1 : {p_win_3_1*100:5.1f}%")
        print(f"  {t1} 3-2 : {p_win_3_2*100:5.1f}%")
        print(f"  {t2} 3-0 : {p_lose_0_3*100:5.1f}%")
        print(f"  {t2} 3-1 : {p_lose_1_3*100:5.1f}%")
        print(f"  {t2} 3-2 : {p_lose_2_3*100:5.1f}%")
    print("=" * 50)

def display_map_score_distribution() -> None:
    """
    Display histogram of map score distribution per team.
    """
    history = load_history()
    if not history:
        print("\n>>> No match history found.")
        return
    
    print("\n=== MAP SCORE DISTRIBUTION ===")
    print(f"  Total matches analyzed: {len(history)}")
    
    team_stats = {}
    
    for m in history:
        t1 = m.get('t1', {})
        t2 = m.get('t2', {})
        s1 = t1.get('score', 0)
        s2 = t2.get('score', 0)
        
        max_maps = 3 if (s1 <= 2 and s2 <= 2) else 5
        
        for side, score in [(t1.get('name'), s1), (t2.get('name'), s2)]:
            if not side: continue
            if side not in team_stats:
                team_stats[side] = {
                    '2-0_wins': 0, '2-1_wins': 0,
                    '3-0_wins': 0, '3-1_wins': 0, '3-2_wins': 0,
                    '0-2_losses': 0, '1-2_losses': 0,
                    '0-3_losses': 0, '1-3_losses': 0, '2-3_losses': 0,
                    'total_matches': 0
                }
            
            team_stats[side]['total_matches'] += 1
            
            opp_score = s2 if side == t1.get('name') else s1
            
            if score > opp_score:
                if max_maps == 3:
                    if score == 2 and opp_score == 0:
                        team_stats[side]['2-0_wins'] += 1
                    elif score == 2 and opp_score == 1:
                        team_stats[side]['2-1_wins'] += 1
                else:
                    if score == 3 and opp_score == 0:
                        team_stats[side]['3-0_wins'] += 1
                    elif score == 3 and opp_score == 1:
                        team_stats[side]['3-1_wins'] += 1
                    elif score == 3 and opp_score == 2:
                        team_stats[side]['3-2_wins'] += 1
            else:
                if max_maps == 3:
                    if score == 0 and opp_score == 2:
                        team_stats[side]['0-2_losses'] += 1
                    elif score == 1 and opp_score == 2:
                        team_stats[side]['1-2_losses'] += 1
                else:
                    if score == 0 and opp_score == 3:
                        team_stats[side]['0-3_losses'] += 1
                    elif score == 1 and opp_score == 3:
                        team_stats[side]['1-3_losses'] += 1
                    elif score == 2 and opp_score == 3:
                        team_stats[side]['2-3_losses'] += 1
    
    print("\n--- Display Options ---")
    print("  1. View Specific Team")
    print("  2. View Top 10 Teams (by ELO Ranking)")
    print("  0. Back")
    
    choice = check_cmd(input("  Select: ")).strip()
    if get_cmd(choice) in ['back', '0']:
        return
    
    if choice == '1':
        team_raw = check_cmd(input("  Enter team name: ")).strip()
        if get_cmd(team_raw) in ['back', '0']:
            return
        found_team = find_team(team_raw)
        if found_team and found_team in team_stats:
            teams_to_show = [found_team]
        else:
            print(f">>> Team '{team_raw}' not found.")
            return
    elif choice == '2':
        ranked_teams = sorted(teams.items(), key=lambda x: x[1], reverse=True)
        teams_to_show = [name for name, elo in ranked_teams if name in team_stats][:10]
        if not teams_to_show:
            print("  [!] No teams found.")
            return
    else:
        print("  [!] Invalid option.")
        return
    
    for idx, team in enumerate(teams_to_show, 1):
        stats = team_stats[team]
        total = stats['total_matches']
        current_elo = int(teams.get(team, 0))
        
        if len(teams_to_show) > 1:
            print(f"\n\n{'='*70}")
            print(f"  #{idx} {team} | ELO: {current_elo} | {total} matches")
        else:
            print(f"\n{'='*70}")
            print(f"  {team} | ELO: {current_elo} | {total} matches")
        print(f"{'='*70}")
        
        total_wins = (stats['2-0_wins'] + stats['2-1_wins'] + 
                     stats['3-0_wins'] + stats['3-1_wins'] + stats['3-2_wins'])
        total_losses = (stats['0-2_losses'] + stats['1-2_losses'] + 
                       stats['0-3_losses'] + stats['1-3_losses'] + stats['2-3_losses'])
        win_rate = (total_wins / total * 100) if total > 0 else 0
        
        print(f"\n  Overall: {total_wins}W - {total_losses}L ({win_rate:.1f}% win rate)")
        
        bo3_total = (stats['2-0_wins'] + stats['2-1_wins'] + 
                    stats['0-2_losses'] + stats['1-2_losses'])
        if bo3_total > 0:
            print(f"\n  --- Best-of-3 Results ({bo3_total} matches) ---\n")
            
            bo3_data = [
                ('2-0', stats['2-0_wins']),
                ('2-1', stats['2-1_wins']),
                ('1-2', stats['1-2_losses']),
                ('0-2', stats['0-2_losses']),
            ]
            
            max_count = max([d[1] for d in bo3_data]) if any(d[1] > 0 for d in bo3_data) else 1
            COL_WIDTH = 8
            
            for row in range(10, 0, -1):
                threshold = (row / 10) * max_count
                
                if row == 10:
                    pct_label = "100%"
                elif row == 8:
                    pct_label = " 80%"
                elif row == 6:
                    pct_label = " 60%"
                elif row == 4:
                    pct_label = " 40%"
                elif row == 2:
                    pct_label = " 20%"
                else:
                    pct_label = "    "
                
                line = f"  {pct_label} |"
                for score, count in bo3_data:
                    if count >= threshold:
                        line += "##".center(COL_WIDTH)
                    else:
                        line += " " * COL_WIDTH
                print(line)
            
            print(f"       +{'-' * (COL_WIDTH * len(bo3_data))}")
            
            label_line = "        "
            count_line = "        "
            for score, count in bo3_data:
                label_line += str(score).center(COL_WIDTH)
                count_line += str(count).center(COL_WIDTH)
            print(label_line)
            print(count_line)
        else:
            print(f"\n  --- Best-of-3 Results (0 matches) ---")
            print("  No Bo3 matches recorded.")
        
        bo5_total = (stats['3-0_wins'] + stats['3-1_wins'] + stats['3-2_wins'] + 
                    stats['0-3_losses'] + stats['1-3_losses'] + stats['2-3_losses'])
        if bo5_total > 0:
            print(f"\n  --- Best-of-5 Results ({bo5_total} matches) ---\n")
            
            bo5_data = [
                ('3-0', stats['3-0_wins']),
                ('3-1', stats['3-1_wins']),
                ('3-2', stats['3-2_wins']),
                ('2-3', stats['2-3_losses']),
                ('1-3', stats['1-3_losses']),
                ('0-3', stats['0-3_losses']),
            ]
            
            max_count = max([d[1] for d in bo5_data]) if any(d[1] > 0 for d in bo5_data) else 1
            COL_WIDTH = 8
            
            for row in range(10, 0, -1):
                threshold = (row / 10) * max_count
                
                if row == 10:
                    pct_label = "100%"
                elif row == 8:
                    pct_label = " 80%"
                elif row == 6:
                    pct_label = " 60%"
                elif row == 4:
                    pct_label = " 40%"
                elif row == 2:
                    pct_label = " 20%"
                else:
                    pct_label = "    "
                
                line = f"  {pct_label} |"
                for score, count in bo5_data:
                    if count >= threshold:
                        line += "##".center(COL_WIDTH)
                    else:
                        line += " " * COL_WIDTH
                print(line)
            
            print(f"       +{'-' * (COL_WIDTH * len(bo5_data))}")
            
            label_line = "        "
            count_line = "        "
            for score, count in bo5_data:
                label_line += str(score).center(COL_WIDTH)
                count_line += str(count).center(COL_WIDTH)
            print(label_line)
            print(count_line)
        else:
            print(f"\n  --- Best-of-5 Results (0 matches) ---")
            print("  No Bo5 matches recorded.")
        
        print(f"\n  --- Dominance Metrics ---")
        clean_wins = stats['2-0_wins'] + stats['3-0_wins'] + stats['3-1_wins']
        close_wins = stats['2-1_wins'] + stats['3-2_wins']
        clean_losses = stats['0-2_losses'] + stats['0-3_losses'] + stats['1-3_losses']
        close_losses = stats['1-2_losses'] + stats['2-3_losses']
        
        if total_wins > 0:
            clean_win_rate = (clean_wins / total_wins * 100)
            close_win_rate = (close_wins / total_wins * 100)
            print(f"    Clean Wins (2-0/3-0/3-1): {clean_wins} ({clean_win_rate:.1f}% of wins)")
            print(f"    Close Wins (2-1/3-2): {close_wins} ({close_win_rate:.1f}% of wins)")
        
        if total_losses > 0:
            clean_loss_rate = (clean_losses / total_losses * 100)
            close_loss_rate = (close_losses / total_losses * 100)
            print(f"    Clean Losses (0-2/0-3/1-3): {clean_losses} ({clean_loss_rate:.1f}% of losses)")
            print(f"    Close Losses (1-2/2-3): {close_losses} ({close_loss_rate:.1f}% of losses)")
    
    print(f"\n{'='*70}")
    if len(teams_to_show) > 1:
        print(f"  Displayed {len(teams_to_show)} teams (ordered by ELO ranking)")
    print("\nPress Enter to return...")
    input()

def duplicate_match_detection():
    """
    Detect and manage duplicate matches.
    """
    history = load_history()
    if len(history) < 2:
        print("\n>>> No match history to check.")
        return

    groups = {}
    for idx, m in enumerate(history):
        t1 = m.get('t1', {}).get('name')
        t2 = m.get('t2', {}).get('name')
        if not t1 or not t2: continue
        team_set = frozenset([t1, t2])
        url = m.get('url', '')

        if url:
            groups.setdefault(('url', url), []).append((idx, m))
            continue

        date_str = m.get('date', 'N/A')
        date_day = date_str.split()[0] if date_str != 'N/A' else None
        key = ('teams_date', team_set, date_day)
        groups.setdefault(key, []).append((idx, m))

    duplicates = [group for group in groups.values() if len(group) >= 2]

    if not duplicates:
        print("\n>>> No duplicate matches found.")
        return

    print(f"\n=== DUPLICATE MATCH DETECTION ===")
    print(f"Found {len(duplicates)} duplicate group(s).")

    to_delete = []

    for grp_idx, matches in enumerate(duplicates, 1):
        print(f"\nGroup {grp_idx}: ({len(matches)} matches)")
        m0 = matches[0][1]
        team1 = m0.get('t1', {}).get('name', 'Unknown')
        team2 = m0.get('t2', {}).get('name', 'Unknown')
        print(f"  Teams: {team1} vs {team2}")
        if m0.get('date'):
            print(f"  Date: {m0.get('date')}")
        else:
            print("  Date: N/A")
        for local_idx, (global_idx, m) in enumerate(matches, 1):
            t1_data = m.get('t1', {})
            t2_data = m.get('t2', {})
            gf_tag = f"[{m.get('match_stage')}]" if m.get('match_stage') else ("[GRAND FINAL]" if m.get('grand_final') else "")
            url_tag = f"URL: {m.get('url', 'N/A')}" if m.get('url') else ""
            print(f"    {local_idx}. (Index {global_idx+1}) {t1_data.get('name')} {t1_data.get('score')} - {t2_data.get('score')} {t2_data.get('name')} {gf_tag} {url_tag}")

        print("  Select matches to DELETE (comma-separated numbers, e.g., '2,3'), or 'skip':")
        resp = input("  > ").strip().lower()
        if resp == 'skip':
            continue
        if resp == 'all':
            confirm = input(f"  Delete ALL {len(matches)} matches in this group? (y/n): ").lower()
            if confirm == 'y':
                for _, (global_idx, _) in enumerate(matches):
                    to_delete.append(global_idx)
            continue

        parts = [p.strip() for p in resp.split(',')]
        selected = []
        for part in parts:
            try:
                n = int(part) - 1
                if 0 <= n < len(matches):
                    selected.append(n)
                else:
                    print(f"    Invalid number {part}, ignoring.")
            except ValueError:
                print(f"    Invalid number {part}, ignoring.")
        if not selected:
            print("  No valid selections, skipping.")
            continue

        print(f"  Selected for deletion: {', '.join(str(i+1) for i in selected)}")
        confirm = input("  Confirm deletion? (y/n): ").lower()
        if confirm == 'y':
            for n in selected:
                global_idx = matches[n][0]
                to_delete.append(global_idx)
        else:
            print("  Skipped.")

    if not to_delete:
        print("\n>>> No matches deleted.")
        return

    matches_to_delete = [history[idx] for idx in sorted(to_delete)]
    deleted_count = len(matches_to_delete)

    delete_and_resimulate(matches_to_delete)

    history = load_history()
    print(f"\n>>> SUCCESS: Deleted {deleted_count} duplicate match(es).")
def custom_point_adjustments() -> None:
    """Manually add or subtract points from a team."""
    print("\nCUSTOM POINT ADJUSTMENTS")
    print("=" * 40)
    
    t_raw = check_cmd(input("Enter team name: ")).strip()
    if get_cmd(t_raw) in ['back', '0']:
        return
    
    team = find_team(t_raw)
    if not team:
        print(">>> Team not found.")
        return
    
    old_pts = teams[team]
    print(f"  Current points for {team}: {int(old_pts)}")
    
    raw_adj = check_cmd(input("Enter adjustment amount (+ or -) or '0' to go back: ")).strip()
    if get_cmd(raw_adj) in ['back', '0']:
        return
    
    try:
        adj = float(raw_adj)
    except ValueError:
        print(">>> Invalid number.")
        return
    
    new_pts = old_pts + adj
    if new_pts < 0:
        print(">>> Warning: Points would go negative. Setting to 0.")
        new_pts = 0
    
    reason = check_cmd(input("Reason for adjustment (optional, or '0' to go back): ")).strip()
    if get_cmd(reason) in ['back', '0']:
        return

    print(f"\nPreview:")
    print(f"  {team}: {int(old_pts)} -> {int(new_pts)} ({adj:+.1f})")
    if reason:
        print(f"  Reason: {reason}")
    
    confirm = get_cmd(check_cmd(input("Confirm? (y/n): ")))
    if confirm != 'y':
        print(">>> Cancelled.")
        return

    teams[team] = new_pts
    mark_unsaved()
    from datetime import datetime
    adj_record = {
        'team': team,
        'date': datetime.now().strftime("%Y-%m-%d %H:%M"),
        'old_points': old_pts,
        'new_points': new_pts,
        'adjustment': adj,
        'reason': reason
    }
    adjustments.append(adj_record)
    save_all()
    print(f">>> Adjusted {team}'s points by {adj:+.1f} to {int(new_pts)}.")
    if reason:
        print(f"    Reason: {reason}")

# =============================================================================
# === EVENT SUMMARY & VRS COMPARISON ===
# =============================================================================

def calculate_form_at_date(team_name: str, cutoff_date: datetime, history: List[Dict[str, Any]]) -> Optional[Tuple[str, float]]:
    """
    Calculate form score for a team using only matches BEFORE cutoff_date.
    
    Parameters:
    - team_name: The team to calculate form for
    - cutoff_date: Only include matches before this date
    - history: List of match records (REQUIRED - no global fallback)
    
    Returns: (grade, score) or None if insufficient data
    """
    if not history:
        return None
    
    team_matches = []
    for m in history:
        match_date_str = m.get('date', 'N/A')
        if match_date_str == 'N/A' or not match_date_str:
            continue
        
        try:
            match_date = datetime.strptime(match_date_str[:10], "%Y-%m-%d").date()
            if match_date >= cutoff_date:
                continue
        except:
            continue
        
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
    
    if total_weight == 0:
        return None
    
    win_rate = win_weighted / total_weight
    map_win_rate = map_wins_weighted / map_total_weighted if map_total_weighted > 0 else 0.5
    comp_rate = comp_weighted / total_weight
    
    win_score = win_rate * FORM_WIN_WEIGHT
    map_score = map_win_rate * FORM_MAP_WEIGHT
    comp_score = comp_rate * FORM_COMP_WEIGHT
    
    score = win_score + map_score + comp_score
    
    total_form_points = FORM_WIN_WEIGHT + FORM_MAP_WEIGHT + FORM_COMP_WEIGHT
    if score >= total_form_points * 0.85:   grade = 'S'
    elif score >= total_form_points * 0.70: grade = 'A'
    elif score >= total_form_points * 0.55: grade = 'B'
    elif score >= total_form_points * 0.40: grade = 'C'
    else:                                   grade = 'D'
    
    return grade, round(score, 1)


def get_all_events():
    """Extract unique event names from history with match counts."""
    events = {}
    for m in history:
        event = get_match_event(m)
        if event and event != 'N/A':
            if event not in events:
                events[event] = []
            events[event].append(m)
    return events


def generate_event_summary(event_name, event_matches=None):
    """
    Generate complete summary for a specific event.
    """
    from datetime import datetime, timedelta
    
    if event_matches is None:
        event_matches = [m for m in history if get_match_event(m) == event_name]
    
    if not event_matches:
        print("\n>>> No matches found for this event.")
        return
    
    sorted_matches = sorted(event_matches, key=lambda m: m.get('date', ''))

    first_match = sorted_matches[0]
    tier = first_match.get('tier', 'N/A')

    env_counts = {}
    for m in sorted_matches:
        match_env = m.get('env', 'UNKNOWN')
        env_counts[match_env] = env_counts.get(match_env, 0) + 1

    env_parts = [f"{env} ({count})" for env, count in sorted(env_counts.items(), key=lambda x: x[1], reverse=True)]
    env_display = " | ".join(env_parts)
    
    dates = []
    for m in sorted_matches:
        date_str = m.get('date', 'N/A')
        if date_str and date_str != 'N/A':
            try:
                dates.append(datetime.strptime(date_str[:10], "%Y-%m-%d").date())
            except:
                pass
    
    if dates:
        start_date = min(dates)
        end_date = max(dates)
        date_range_str = f"{start_date} to {end_date}"
    else:
        date_range_str = "Unknown"
    
    team_stats = {}
    
    for m in sorted_matches:
        for side in ['t1', 't2']:
            team = m.get(side, {}).get('name')
            if not team:
                continue
            
            pts_before = m.get(side, {}).get('pts_before')
            pts_after = m.get(side, {}).get('pts_after')
            
            if pts_before is None or pts_after is None:
                continue
            
            if team not in team_stats:
                team_stats[team] = {
                    'start_rating': float(pts_before),
                    'end_rating': float(pts_after),
                    'wins': 0, 'losses': 0,
                    'maps_won': 0, 'maps_lost': 0,
                    'matches': 0,
                    'form_start': None,
                    'form_end': None
                }
            
            team_stats[team]['matches'] += 1
            team_stats[team]['end_rating'] = float(pts_after)
            
            opp_side = 't2' if side == 't1' else 't1'
            team_score = m.get(side, {}).get('score', 0)
            opp_score = m.get(opp_side, {}).get('score', 0)
            
            if team_score > opp_score:
                team_stats[team]['wins'] += 1
            else:
                team_stats[team]['losses'] += 1
            
            team_stats[team]['maps_won'] += team_score
            team_stats[team]['maps_lost'] += opp_score
    
    if not team_stats:
        print("\n>>> ERROR: No teams with valid rating data found!")
        print("    Try resimulating match history from Match History menu.")
        print("\nPress Enter to return...")
        input()
        return
    
    team_first_match_date = {}
    for m in sorted_matches:
        date_str = m.get('date', 'N/A')
        if date_str and date_str != 'N/A':
            try:
                match_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                for side in ['t1', 't2']:
                    team = m.get(side, {}).get('name')
                    if team and team not in team_first_match_date:
                        team_first_match_date[team] = match_date
            except:
                pass
    
    for team, stats in team_stats.items():
        if team in team_first_match_date:
            form_date = team_first_match_date[team]
            stats['form_start'] = calculate_form_at_date(team, form_date, history)
    
    for team, stats in team_stats.items():
        if end_date:
            form_after_cutoff = end_date + timedelta(days=1)
            stats['form_end'] = calculate_form_at_date(team, form_after_cutoff, history)
    
    for team, stats in team_stats.items():
        stats['rating_change'] = stats['end_rating'] - stats['start_rating']
        stats['map_diff'] = stats['maps_won'] - stats['maps_lost']
        
        if stats['form_start'] and stats['form_end']:
            stats['form_change'] = stats['form_end'][1] - stats['form_start'][1]
        else:
            stats['form_change'] = None
    
    grand_final = [m for m in sorted_matches if m.get('grand_final', False)]
    
    print("\n" + "-" * 92)
    print(f"  EVENT SUMMARY: {event_name}")
    print("-" * 92)
    print(f"  Tier: {tier}  |  Environments: {env_display}  |  Matches: {len(sorted_matches)}  |  Dates: {date_range_str}")
    print("-" * 92)
    
    print("\nTOP RATING GAINERS")
    print("-" * 80)
    
    gainers = [(team, stats) for team, stats in team_stats.items() if stats['rating_change'] > 0]
    gainers.sort(key=lambda x: x[1]['rating_change'], reverse=True)
    
    if gainers:
        print(f"  {'#':<3} {'Team':<22} {'Rating Change':<18} {'Record'}")
        print(f"  {'-'*55}")
        for i, (team, stats) in enumerate(gainers[:5], 1):
            rating_str = f"{int(stats['start_rating'])} -> {int(stats['end_rating'])} ({stats['rating_change']:+.0f})"
            record = f"{stats['wins']}-{stats['losses']}"
            print(f"  {i:<3} {team:<22} {rating_str:<18} {record}")
    else:
        print("  No teams gained rating at this event.")
    
    print("\nTOP RATING LOSERS")
    print("-" * 80)
    
    losers = [(team, stats) for team, stats in team_stats.items() if stats['rating_change'] < 0]
    losers.sort(key=lambda x: x[1]['rating_change'])
    
    if losers:
        print(f"  {'#':<3} {'Team':<22} {'Rating Change':<18} {'Record'}")
        print(f"  {'-'*55}")
        for i, (team, stats) in enumerate(losers[:5], 1):
            rating_str = f"{int(stats['start_rating'])} -> {int(stats['end_rating'])} ({stats['rating_change']:+.0f})"
            record = f"{stats['wins']}-{stats['losses']}"
            print(f"  {i:<3} {team:<22} {rating_str:<18} {record}")
    else:
        print("  No teams lost rating at this event.")
    
    print("\nTOP FORM IMPROVERS")
    print("-" * 80)
    
    form_improvers = [(team, stats) for team, stats in team_stats.items() 
                      if stats['form_change'] is not None and stats['form_change'] > 0]
    form_improvers.sort(key=lambda x: x[1]['form_change'], reverse=True)
    
    if form_improvers:
        print(f"  {'#':<3} {'Team':<22} {'Form Change':<22} {'Record'}")
        print(f"  {'-'*60}")
        for i, (team, stats) in enumerate(form_improvers[:5], 1):
            fs_grade, fs_score = stats['form_start']
            fe_grade, fe_score = stats['form_end']
            form_str = f"{fs_grade} {int(fs_score)} -> {fe_grade} {int(fe_score)} ({stats['form_change']:+.0f})"
            record = f"{stats['wins']}-{stats['losses']}"
            print(f"  {i:<3} {team:<22} {form_str:<22} {record}")
    else:
        print("  No teams improved form at this event.")
    
    print("\nTOP FORM DECLINERS")
    print("-" * 80)
    
    form_decliners = [(team, stats) for team, stats in team_stats.items() 
                      if stats['form_change'] is not None and stats['form_change'] < 0]
    form_decliners.sort(key=lambda x: x[1]['form_change'])
    
    if form_decliners:
        print(f"  {'#':<3} {'Team':<22} {'Form Change':<22} {'Record'}")
        print(f"  {'-'*60}")
        for i, (team, stats) in enumerate(form_decliners[:5], 1):
            fs_grade, fs_score = stats['form_start']
            fe_grade, fe_score = stats['form_end']
            form_str = f"{fs_grade} {int(fs_score)} -> {fe_grade} {int(fe_score)} ({stats['form_change']:+.0f})"
            record = f"{stats['wins']}-{stats['losses']}"
            print(f"  {i:<3} {team:<22} {form_str:<22} {record}")
    else:
        print("  No teams declined in form at this event.")
    
    print("\nEVENT STATISTICS")
    print("-" * 80)
    
    if team_stats:
        highest_team = max(team_stats.items(), key=lambda x: x[1]['end_rating'])
        print(f"  - Highest Rated Team: {highest_team[0]} ({int(highest_team[1]['end_rating'])} pts)")
    
    upsets = []
    for m in sorted_matches:
        t1 = m.get('t1', {})
        t2 = m.get('t2', {})
        pts1_before = t1.get('pts_before', 0)
        pts2_before = t2.get('pts_before', 0)
        pts1_after = t1.get('pts_after', 0)
        pts2_after = t2.get('pts_after', 0)
        s1 = t1.get('score', 0)
        s2 = t2.get('score', 0)
        
        if pts1_before and pts2_before:
            if s1 > s2 and pts1_before < pts2_before:
                upsets.append({
                    'diff': pts2_before - pts1_before,
                    'winner': t1.get('name'),
                    'loser': t2.get('name'),
                    'score': f"{s1}-{s2}",
                    'winner_change': pts1_after - pts1_before,
                    'loser_change': pts2_after - pts2_before
                })
            elif s2 > s1 and pts2_before < pts1_before:
                upsets.append({
                    'diff': pts1_before - pts2_before,
                    'winner': t2.get('name'),
                    'loser': t1.get('name'),
                    'score': f"{s2}-{s1}",
                    'winner_change': pts2_after - pts2_before,
                    'loser_change': pts1_after - pts1_before
                })
    
    if upsets:
        upsets.sort(key=lambda x: x['diff'], reverse=True)
        biggest = upsets[0]
        print(f"  - Biggest Upset: {biggest['winner']} def. {biggest['loser']}")
        print(f"      Rating Gap: {int(biggest['diff'])} pts | Score: {biggest['score']}")
        print(f"      Rating Changes: {biggest['winner']} {biggest['winner_change']:+.0f} | {biggest['loser']} {biggest['loser_change']:+.0f}")
    
    if team_stats:
        most_maps = max(team_stats.items(), key=lambda x: x[1]['maps_won'] + x[1]['maps_lost'])
        total_maps = most_maps[1]['maps_won'] + most_maps[1]['maps_lost']
        print(f"  - Most Maps Played: {most_maps[0]} ({total_maps} maps across {most_maps[1]['matches']} matches)")
    
    all_changes = [stats['rating_change'] for stats in team_stats.values()]
    avg_change = sum(all_changes) / len(all_changes) if all_changes else 0
    print(f"  - Average Rating Change: {avg_change:+.1f} pts per team")
    
    form_changes = [stats['form_change'] for stats in team_stats.values() if stats['form_change'] is not None]
    if form_changes:
        avg_form_change = sum(form_changes) / len(form_changes)
        print(f"  - Average Form Change: {avg_form_change:+.1f} points")
    
    if grand_final:
        gf = grand_final[0]
        t1 = gf.get('t1', {}).get('name', 'Unknown')
        t2 = gf.get('t2', {}).get('name', 'Unknown')
        s1 = gf.get('t1', {}).get('score', 0)
        s2 = gf.get('t2', {}).get('score', 0)
        winner = t1 if s1 > s2 else t2
        loser = t2 if winner == t1 else t1
        print(f"\nGRAND FINAL: {winner} def. {loser} ({max(s1,s2)}-{min(s1,s2)})")
    
    print("\n" + "=" * 80)
    print("\nPress Enter to return...")
    input()


def event_summary_menu() -> None:
    """Menu to select and view event summaries."""
    events = get_all_events()
    
    if not events:
        print("\n>>> No events found in match history.")
        return
    
    while True:
        print("\n" + "-" * 22)
        print("  EVENT SUMMARY MENU")
        print("-" * 22)
        print(f"  Total Events: {len(events)}")
        print("-" * 91)
        
        from datetime import datetime
        
        event_info = []
        for event, matches in events.items():
            dates = [datetime.strptime(m.get('date', 'N/A')[:10], "%Y-%m-%d").date() 
                     for m in matches if m.get('date') and m.get('date') != 'N/A']
            
            if dates:
                date_range = f"{min(dates)} to {max(dates)}"
                sort_date = max(dates)
            else:
                date_range = "Unknown dates"
                sort_date = None
            
            event_info.append({'name': event, 'matches': len(matches), 'date_range': date_range, 'sort_date': sort_date})
        
        event_info.sort(key=lambda x: x['sort_date'] if x['sort_date'] else datetime.min.date(), reverse=True)
        
        page_size = 9
        total_pages = (len(event_info) + page_size - 1) // page_size
        
        for page in range(total_pages):
            start_idx = page * page_size
            end_idx = min(start_idx + page_size, len(event_info))
            
            for i, info in enumerate(event_info[start_idx:end_idx], start_idx + 1):
                event_name = info['name'][:45] if len(info['name']) > 45 else info['name']
                print(f"  {i:<3} {event_name:<45} ({info['matches']:>2} matches) - {info['date_range']}")
            
            if page < total_pages - 1:
                print(f"  ... ({len(event_info) - end_idx} more events on next page)")
                print("-" * 91)
                next_page = input("  Press Enter for next page, or '0' to go back: ").strip()
                if next_page == '0':
                    break
                print()
        
        print("-" * 91)
        print("  0. Back")
        print()
        
        choice = input("  Select event number: ").strip()
        
        if choice == '0':
            break
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(event_info):
                generate_event_summary(event_info[idx]['name'], events[event_info[idx]['name']])
            else:
                print("  [!] Invalid selection.")
        except ValueError:
            print("  [!] Please enter a number.")

def compare_csrs_to_vrs():
    """
    Compare CSRS Elo ratings against official Valve Ranking System points.
    Only shows teams above ELITE_THRESHOLD (850+).
    """
    print("\n=== CSRS vs VRS COMPARISON ===")
    print("Fetching VRS rankings from HLTV...")
    
    # Get elite teams only
    elite_teams = [name for name, pts in teams.items() if pts >= ELITE_THRESHOLD]
    elite_teams.sort(key=lambda x: teams[x], reverse=True)
    
    if not elite_teams:
        print(f">>> No teams above elite threshold ({ELITE_THRESHOLD}+).")
        return
    
    print(f"\nElite Teams Found: {len(elite_teams)} (CSRS Elo >= {ELITE_THRESHOLD})\n")
    
    results = []
    vrs_fetch_date = datetime.now().strftime("%Y-%m-%d")
    
    for team in elite_teams:
        csrs_elo = teams[team]
        
        # Try to fetch VRS points (uses existing cache)
        vrs_pts = scrape_vrs_points(team)
        
        if vrs_pts is not None:
            vrs_converted = vrs_pts / 2
            diff = csrs_elo - vrs_converted
            
            # Determine status - FIXED: single icon, not duplicated
            if abs(diff) <= 25:
                status = "Aligned"
                status_icon = "[OK]"
            elif diff > 25:
                status = "Overrated"
                status_icon = "[*]"
            else:
                status = "Underrated"
                status_icon = "[!]"
        else:
            vrs_converted = None
            diff = None
            status = "No VRS Data"
            status_icon = "[--]"
        
        results.append({
            'team': team,
            'csrs': csrs_elo,
            'vrs': vrs_pts,
            'vrs_conv': vrs_converted,
            'diff': diff,
            'status': status,
            'status_icon': status_icon
        })
    
    # Display table
    print(f"VRS Data From: {vrs_fetch_date} (HLTV Valve Ranking)")
    print(f"Conversion: VRS Points / 2 = CSRS Elo Equivalent\n")
    
    print(f"  {'#':<3} {'Team':<22} {'CSRS':<8} {'VRS':<8} {'VRS/2':<8} {'Diff':<8} {'Status'}")
    print(f"  {'-'*75}")
    
    aligned = 0
    overrated = 0
    underrated = 0
    no_data = 0
    total_diff = 0
    diff_count = 0
    
    for i, r in enumerate(results, 1):
        vrs_str = str(int(r['vrs'])) if r['vrs'] else "--"
        conv_str = str(int(r['vrs_conv'])) if r['vrs_conv'] else "--"
        diff_str = f"{int(r['diff']):+d}" if r['diff'] is not None else "--"
        
        # FIXED: Only print status_icon once, followed by status text
        print(f"  {i:<3} {r['team'][:22]:<22} {int(r['csrs']):<8} {vrs_str:<8} {conv_str:<8} {diff_str:<8} {r['status_icon']} {r['status']}")
        
        # Track stats
        if r['status'] == "Aligned":
            aligned += 1
        elif r['status'] == "Overrated":
            overrated += 1
        elif r['status'] == "Underrated":
            underrated += 1
        else:
            no_data += 1
        
        if r['diff'] is not None:
            total_diff += r['diff']
            diff_count += 1
    
    print(f"  {'-'*75}")
    
    # Summary stats
    avg_diff = total_diff / diff_count if diff_count > 0 else 0
    
    print(f"\n  Summary: {aligned} Aligned | {overrated} Overrated | {underrated} Underrated | {no_data} No VRS Data")
    
    if diff_count > 0:
        if avg_diff > 25:
            print(f"  Average Diff: {avg_diff:+.1f} (CSRS inflated vs VRS)")
        elif avg_diff < -25:
            print(f"  Average Diff: {avg_diff:+.1f} (CSRS deflated vs VRS)")
        else:
            print(f"  Average Diff: {avg_diff:+.1f} (CSRS well-calibrated)")
    
    print(f"\n  Note: VRS uses ~2-month window | CSRS uses all-time history with depreciation")
    print("\nPress Enter to return...")
    input()

def compare_csrs_vrs_rankings():
    """
    Compare CSRS rankings against VRS rankings for top X teams.
    Shows ACTUAL VRS ranking positions (from full HLTV VRS list).
    """
    print("\n=== CSRS vs VRS RANKINGS COMPARISON ===")
    print("Fetching FULL VRS rankings from HLTV...")
    
    while True:
        try:
            top_x = int(check_cmd(input("Compare top how many CSRS teams? (5-50): ")))
            if 5 <= top_x <= 50:
                break
            print("  [!] Please enter a number between 5 and 50.")
        except ValueError:
            print("  [!] Invalid number.")
    
    csrs_ranked = get_sorted_rankings()[:top_x]
    
    if not csrs_ranked:
        print(">>> No teams found.")
        return
    
    print(f"\nFetching FULL VRS ranking list to get actual positions...\n")
    
    vrs_full_rankings = {}
    vrs_fetch_date = datetime.now().strftime("%Y-%m-%d")
    
    try:
        from datetime import date as date_cls
        today = date_cls.today()
        vrs_url = f"https://www.hltv.org/valve-ranking/teams/{today.year}/{today.strftime('%B').lower()}/{today.day}"

        with BrowserSession() as sess:
            page = sess.new_page()
            try:
                page.goto(vrs_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                vrs_data = page.evaluate("""() => {
                    const results = {};
                    const entries = document.querySelectorAll('.ranking-header');
                    console.log('Found ranking-header entries:', entries.length);
                    for (let i = 0; i < entries.length; i++) {
                        const entry = entries[i];
                        if (entry.innerHTML.indexOf('old-roster') !== -1) continue;
                        const positionEl = entry.querySelector('.position');
                        if (!positionEl) continue;
                        const rankMatch = positionEl.textContent.trim().match(/#?(\\d+)/);
                        if (!rankMatch) continue;
                        const rank = parseInt(rankMatch[1]);
                        const nameEl = entry.querySelector('.teamLine .name');
                        if (!nameEl) continue;
                        const teamName = nameEl.textContent.trim().toLowerCase();
                        if (!teamName || teamName.length < 2) continue;
                        const pointsEl = entry.querySelector('.points');
                        if (!pointsEl) continue;
                        const pointsMatch = pointsEl.textContent.trim().match(/(\\d+)\\s*(?:Valve\\s*points)?/);
                        if (!pointsMatch) continue;
                        const points = parseFloat(pointsMatch[1]);
                        if (rank && teamName && points) {
                            results[teamName] = {'rank': rank, 'points': points};
                        }
                    }
                    return results;
                }""")

                for team_name_lower, data in vrs_data.items():
                    vrs_full_rankings[team_name_lower] = data['rank']

                print(f"  [OK] Fetched {len(vrs_full_rankings)} teams from VRS\n")
            finally:
                try: page.close()
                except: pass

    except Exception as e:
        print(f"  [!] VRS scrape error: {e}")
        log_scrape_error("VRS Rankings", vrs_url if 'vrs_url' in locals() else "Unknown", str(e))
    
    # === BUILD RESULTS WITH ACTUAL VRS RANKS ===
    results = []
    
    for csrs_rank, team in enumerate(csrs_ranked, 1):
        csrs_elo = teams[team]
        vrs_rank = vrs_full_rankings.get(team.lower())
        vrs_pts = scrape_vrs_points(team)
        vrs_converted = vrs_pts / 2 if vrs_pts else None
        
        results.append({
            'csrs_rank': csrs_rank,
            'team': team,
            'csrs_elo': csrs_elo,
            'vrs_pts': vrs_pts,
            'vrs_converted': vrs_converted,
            'vrs_rank': vrs_rank
        })
    
    # Display table
    print(f"VRS Data From: {vrs_fetch_date} (HLTV Valve Ranking)")
    print(f"Conversion: VRS Points / 2 = CSRS Elo Equivalent\n")
    
    print(f"  {'CSRS #':<6} {'VRS #':<6} {'Diff':<6} {'Team':<22} {'CSRS':<8} {'VRS/2':<8}")
    print(f"  {'-'*85}")
    
    same_rank = higher_csrs = higher_vrs = no_vrs_data = 0
    
    for r in results:
        vrs_str = str(int(r['vrs_converted'])) if r['vrs_converted'] else "--"
        
        if r['vrs_rank'] is not None:
            rank_diff = r['csrs_rank'] - r['vrs_rank']
            if rank_diff == 0:
                diff_str, diff_icon = "─", "[=]"
                same_rank += 1
            elif rank_diff > 0:
                diff_str, diff_icon = f"-{rank_diff}", "[↓]"
                higher_vrs += 1
            else:
                diff_str, diff_icon = f"+{abs(rank_diff)}", "[↑]"
                higher_csrs += 1
            
            print(f"  {r['csrs_rank']:<6} {r['vrs_rank']:<6} {diff_str:<6} {r['team'][:22]:<22} {int(r['csrs_elo']):<8} {vrs_str:<8} {diff_icon}")
        else:
            diff_str, diff_icon = "--", "[--]"
            no_vrs_data += 1
            print(f"  {r['csrs_rank']:<6} {'--':<6} {diff_str:<6} {r['team'][:22]:<22} {int(r['csrs_elo']):<8} {vrs_str:<8} {diff_icon}")
    
    print(f"  {'-'*85}")
    
    print(f"\n  Summary:")
    print(f"    Same Rank:      {same_rank} teams ({same_rank/len(results)*100:.1f}%)")
    print(f"    Higher in CSRS: {higher_csrs} teams ({higher_csrs/len(results)*100:.1f}%)")
    print(f"    Higher in VRS:  {higher_vrs} teams ({higher_vrs/len(results)*100:.1f}%)")
    print(f"    No VRS Data:    {no_vrs_data} teams")
    
    teams_with_diff = [r for r in results if r['vrs_rank'] is not None]
    if teams_with_diff:
        biggest_csrs_high = max(teams_with_diff, key=lambda x: x['csrs_rank'] - x['vrs_rank'])
        biggest_vrs_high = min(teams_with_diff, key=lambda x: x['csrs_rank'] - x['vrs_rank'])
        
        print(f"\n  Notable Differences:")
        if biggest_csrs_high['csrs_rank'] - biggest_csrs_high['vrs_rank'] > 0:
            print(f"    Highest in VRS: {biggest_csrs_high['team']} (VRS #{biggest_csrs_high['vrs_rank']} vs CSRS #{biggest_csrs_high['csrs_rank']})")
        if biggest_vrs_high['csrs_rank'] - biggest_vrs_high['vrs_rank'] < 0:
            print(f"    Highest in CSRS: {biggest_vrs_high['team']} (CSRS #{biggest_vrs_high['csrs_rank']} vs VRS #{biggest_vrs_high['vrs_rank']})")
    
    print(f"\n  Note: VRS uses ~2-month window | CSRS uses all-time history with depreciation")
    print("\nPress Enter to return...")
    input()

# === BROWSER SESSION ===
# =============================================================================

_BROWSER_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# Pool of realistic UAs rotated per BrowserSession to avoid fingerprint
# consistency that Cloudflare flags on repeated headless visits.
_BROWSER_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

def _pick_ua() -> str:
    """Pick a random UA from the pool each call."""
    import random
    return random.choice(_BROWSER_UA_POOL)

_BROWSER_DEFAULT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-features=IsolateOrigins,site-per-process",
    "--window-size=1920,1080",
    "--start-maximized",
]

_CAMOUFOX_HEADLESS = True

# Path to a JSON cookie file exported from a real logged-in HLTV browser
# session (e.g. via the "Cookie-Editor" extension → Export → JSON).
# If the file exists, cookies are loaded into every BrowserSession, which
# makes Cloudflare treat the scraper as an authenticated user.
# Leave as None or point to a non-existent path to skip.
HLTV_COOKIE_FILE = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "data", "hltv_cookies.json")

def _load_hltv_cookies() -> list:
    """Load HLTV cookies from JSON file if it exists, else return empty list."""
    import json
    if not os.path.exists(HLTV_COOKIE_FILE):
        return []
    try:
        with open(HLTV_COOKIE_FILE) as f:
            raw = json.load(f)
        # Normalise both Cookie-Editor format and plain list-of-dicts
        cookies = []
        for c in raw:
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", ".hltv.org"),
                "path":   c.get("path", "/"),
            }
            if cookie["name"] and cookie["value"]:
                cookies.append(cookie)
        if cookies:
            print(f"[Browser] Loaded {len(cookies)} HLTV cookies from {HLTV_COOKIE_FILE}")
        return cookies
    except Exception as e:
        print(f"[Browser] Failed to load cookie file: {e}")
        return []





class _ThreadSafeProxy:
    """
    Wraps a Camoufox/Playwright object (context, page, etc.) so that every
    attribute access that returns a callable is automatically dispatched
    through the BrowserSession worker thread.  This lets all existing code
    that calls context.new_page(), page.goto(), page.evaluate(), etc. work
    unchanged even when the real object lives on a different thread.
    """
    def __init__(self, obj, run_in_thread):
        # Use object.__setattr__ to avoid triggering our own __setattr__
        object.__setattr__(self, "_obj",           obj)
        object.__setattr__(self, "_run_in_thread", run_in_thread)

    def __getattr__(self, name):
        obj          = object.__getattribute__(self, "_obj")
        run_in_thread = object.__getattribute__(self, "_run_in_thread")
        attr = getattr(obj, name)
        if not callable(attr):
            return attr
        def _dispatch(*args, **kwargs):
            result = run_in_thread(lambda: getattr(obj, name)(*args, **kwargs))
            # Wrap returned contexts/pages/etc. in the same proxy
            _proxiable = ("BrowserContext", "Page", "Frame", "ElementHandle",
                          "JSHandle", "Response", "Request", "Route",
                          "WebSocket", "Worker", "CDPSession", "Browser")
            if result is not None and type(result).__name__ in _proxiable:
                return _ThreadSafeProxy(result, run_in_thread)
            return result
        return _dispatch

    # Forward item access (e.g. dict-like results) transparently
    def __getitem__(self, key):
        return object.__getattribute__(self, "_obj")[key]

    def __iter__(self):
        return iter(object.__getattribute__(self, "_obj"))

    def __bool__(self):
        return bool(object.__getattribute__(self, "_obj"))

    def __repr__(self):
        return f"<_ThreadSafeProxy wrapping {object.__getattribute__(self, '_obj')!r}>"


class BrowserSession:
    """
    Holds one browser instance + context, reused across many page loads.
    Uses Camoufox (Firefox-based, randomised fingerprint) to bypass
    Cloudflare TLS/JA3 fingerprinting that blocks Playwright Chromium.

    Falls back to Playwright Chromium if Camoufox is not installed.

    Usage:
        with BrowserSession() as session:
            for url in urls:
                match = scrape_match_data(url, context=session.context)
                ...
    """

    def __init__(self, headless: bool = True):
        self.headless        = headless
        self._camoufox       = None   # Camoufox context manager
        self._playwright     = None   # fallback
        self.browser         = None
        self.context         = None
        self._stealth_fn     = None
        self._cf_thread      = None   # persistent thread owning Camoufox
        self._cf_queue       = None   # queue to send callables into that thread
        self._cf_stop_event  = None   # signals thread to exit

    def _run_in_cf_thread(self, fn):
        """Submit a callable to the persistent Camoufox thread and return its result."""
        import queue
        result_q = queue.Queue()
        def _task():
            try:
                result_q.put(("ok", fn()))
            except Exception as exc:
                result_q.put(("err", exc))
        self._cf_queue.put(_task)
        tag, val = result_q.get()
        if tag == "err":
            raise val
        return val

    def start(self) -> "BrowserSession":
        if self.context is not None:
            return self  # already started

        # ── Try Camoufox first ──
        # Camoufox's sync API must live entirely on one thread. If we're inside
        # an asyncio loop (which owns the main thread) we spin up a *persistent*
        # worker thread that never exits until stop() is called, so Camoufox's
        # internal event loop always finds its home thread alive.
        _inside_asyncio = False
        try:
            import asyncio
            asyncio.get_running_loop()
            _inside_asyncio = True
        except RuntimeError:
            pass

        try:
            import queue, threading
            from camoufox.sync_api import Camoufox

            # Cookies to load — fetch on main thread before handing off
            cookies = _load_hltv_cookies()
            if not cookies:
                cookies = [{"name": "cookieConsent", "value": "1",
                            "domain": ".hltv.org", "path": "/"}]

            if _inside_asyncio:
                cf_queue      = queue.Queue()
                stop_event    = threading.Event()
                self._cf_queue      = cf_queue
                self._cf_stop_event = stop_event

                def _cf_worker():
                    # Use local refs — self attrs may be None'd by stop() before
                    # this loop checks them again.
                    while not stop_event.is_set():
                        try:
                            task = cf_queue.get(timeout=0.2)
                            task()
                        except queue.Empty:
                            continue

                self._cf_thread = threading.Thread(target=_cf_worker, daemon=True)
                self._cf_thread.start()

                _cookies = cookies  # capture for closure
                def _launch():
                    cf  = Camoufox(headless=self.headless, os=("windows", "macos", "linux"))
                    b   = cf.__enter__()
                    ctx = b.new_context()
                    ctx.add_cookies(_cookies)   # must happen on this thread
                    return cf, b, ctx

                self._camoufox, self.browser, self.context = self._run_in_cf_thread(_launch)
                # Wrap context in a proxy so callers on the main thread are
                # automatically dispatched to the worker thread
                self.context = _ThreadSafeProxy(self.context, self._run_in_cf_thread)
            else:
                # No asyncio loop — launch directly on the current thread
                self._camoufox = Camoufox(headless=self.headless, os=("windows", "macos", "linux"))
                self.browser   = self._camoufox.__enter__()
                self.context   = self.browser.new_context()
                self.context.add_cookies(cookies)

            print("[Browser] Using Camoufox (anti-fingerprint Firefox)")
            return self
        except ImportError:
            pass
        except Exception as e:
            # Clean up thread if launch failed
            if self._cf_stop_event:
                self._cf_stop_event.set()
            if self._cf_thread and self._cf_thread.is_alive():
                self._cf_thread.join(timeout=3)
            self._cf_thread = self._cf_queue = self._cf_stop_event = None
            print(f"[Browser] Camoufox failed ({e}), falling back to Playwright")

        # ── Fallback: Playwright Chromium (run in thread to avoid asyncio conflict) ──
        import threading
        import concurrent.futures

        def _launch_playwright():
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            b = pw.chromium.launch(
                headless=self.headless,
                args=_BROWSER_DEFAULT_LAUNCH_ARGS,
            )
            ua = _pick_ua()
            ctx = b.new_context(
                user_agent=ua,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="dark",
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            return pw, b, ctx

        # Run outside any asyncio loop
        try:
            import asyncio
            asyncio.get_running_loop()
            # We're inside an asyncio loop — launch in a separate thread
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                self._playwright, self.browser, self.context = ex.submit(_launch_playwright).result()
        except RuntimeError:
            # No running loop — safe to launch directly
            self._playwright, self.browser, self.context = _launch_playwright()

        cookies = _load_hltv_cookies()
        if not cookies:
            cookies = [{"name": "cookieConsent", "value": "1",
                        "domain": ".hltv.org", "path": "/"}]
        self.context.add_cookies(cookies)
        self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
        try:
            from playwright_stealth import stealth_sync as _stealth
            self._stealth_fn = _stealth
        except ImportError:
            pass
        print("[Browser] Using Playwright Chromium (Camoufox not available)")
        return self

    def new_page(self):
        """Get a fresh page with stealth applied and a random human-like delay."""
        import random, time
        if self.context is None:
            self.start()
        page = self.context.new_page()
        if self._stealth_fn:
            self._stealth_fn(page)
        time.sleep(random.uniform(1.5, 4.0))
        return page

    def stop(self) -> None:
        """Close everything. Safe to call multiple times."""
        if self._cf_thread is not None:
            # All Camoufox teardown must happen on the worker thread.
            # Unwrap proxy to get the real underlying context object.
            _raw_ctx = (object.__getattribute__(self.context, "_obj")
                        if isinstance(self.context, _ThreadSafeProxy) else self.context)
            ctx, browser, cf = _raw_ctx, self.browser, self._camoufox
            def _teardown():
                for fn in (
                    lambda: ctx.close()               if ctx     else None,
                    lambda: browser.close()           if browser else None,
                    lambda: cf.__exit__(None,None,None) if cf    else None,
                ):
                    try: fn()
                    except Exception: pass
            try:
                self._run_in_cf_thread(_teardown)
            except Exception:
                pass
            # Now signal the worker to exit and wait for it
            if self._cf_stop_event:
                self._cf_stop_event.set()
            if self._cf_thread.is_alive():
                self._cf_thread.join(timeout=5)
        else:
            # No worker thread — close directly
            for closer in (
                lambda: self.context.close()               if self.context    else None,
                lambda: self.browser.close()               if self.browser    else None,
                lambda: self._camoufox.__exit__(None,None,None) if self._camoufox else None,
                lambda: self._playwright.stop()            if self._playwright else None,
            ):
                try: closer()
                except Exception: pass
        self.context        = None
        self.browser        = None
        self._camoufox      = None
        self._playwright    = None
        self._cf_thread     = None
        self._cf_queue      = None
        self._cf_stop_event = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop()
        return False

    @property
    def is_active(self) -> bool:
        return self.context is not None



# === WEB SCRAPING (VRS & HLTV) ===
# =============================================================================

_vrs_cache = {}
_vrs_session_cache = {}
VRS_CACHE_VERSION = 2  # Increment when cache structure changes

# Add error logging helper
def log_scrape_error(source, url, error):
    """Log scraping errors to file for debugging."""
    try:
        from datetime import datetime
        with open(os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "errors", "scrape_errors.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {source} | {url} | {error}\n")
    except Exception:
        pass

def scrape_vrs_points(team_name, match_date=None, context=None):
    """
    Fetch VRS points with corrected selectors, error logging, and cache versioning.

    Parameters:
    - context: optional Playwright BrowserContext to reuse (from BrowserSession).
               If None, launches and tears down its own browser (original standalone behaviour).
    """
    try:
        from datetime import timedelta, date as date_cls
        if match_date:
            day_before = match_date - timedelta(days=1)
            vrs_url = f"https://www.hltv.org/valve-ranking/teams/{day_before.year}/{day_before.strftime('%B').lower()}/{day_before.day}"
            cache_key = str(day_before)
        else:
            today = date_cls.today()
            vrs_url = f"https://www.hltv.org/valve-ranking/teams/{today.year}/{today.strftime('%B').lower()}/{today.day}"
            cache_key = str(today)

        # FIXED: Check cache version before using cached data
        if cache_key in _vrs_cache:
            cached_data = _vrs_cache[cache_key]
            if isinstance(cached_data, dict) and cached_data.get('version') == VRS_CACHE_VERSION:
                pts = cached_data.get('teams', {}).get(team_name.lower())
                if pts is not None:
                    return pts
            # Old cache format - still use it but will update on next scrape
            elif isinstance(cached_data, dict):
                pts = cached_data.get(team_name.lower())
                if pts is not None:
                    return pts
            return None

        owns_browser = context is None
        _sess = None
        try:
            if owns_browser:
                _sess = BrowserSession()
                _sess.start()
                context = _sess.context
            page = context.new_page()

            page.goto(vrs_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector(".ranked-team", state="attached", timeout=15000)
            page.wait_for_timeout(2000)
            
            rankings = page.evaluate("""() => {
                const results = {};
                const entries = document.querySelectorAll('.ranked-team');
                
                for (let i = 0; i < entries.length; i++) {
                    const entry = entries[i];
                    
                    if (entry.innerHTML.indexOf('old-roster') !== -1) {
                        continue;
                    }
                    
                    const ptsEl = entry.querySelector('span.points');
                    if (!ptsEl) continue;
                    
                    const ptsText = ptsEl.textContent.trim();
                    const ptsMatch = ptsText.match(/\\((\\d+)\\s*Valve\\s*points\\)/);
                    if (!ptsMatch) continue;
                    const pts = parseFloat(ptsMatch[1]);
                    
                    const fullText = entry.textContent.trim();
                    const nameMatch = fullText.match(/#\\d+\\s+([^(]+)\\s*\\(/);
                    if (!nameMatch) continue;
                    const name = nameMatch[1].trim().toLowerCase();
                    
                    if (name && pts) {
                        results[name] = pts;
                    }
                }
                return results;
            }""")
            
            _vrs_cache[cache_key] = {
                'version': VRS_CACHE_VERSION,
                'timestamp': datetime.now().isoformat(),
                'teams': rankings,
                'url': vrs_url
            }
            
            pts = rankings.get(team_name.lower())
            if pts is not None:
                return pts
            return None
            
        finally:
            try:
                if page: page.close()
            except: pass
            if owns_browser and _sess:
                _sess.stop()
            
    except Exception as e:
        # FIXED: Log error to file
        log_scrape_error("VRS", vrs_url if 'vrs_url' in locals() else "Unknown", str(e))
        print(f"  [!] VRS scrape error: {e}")
    return None

def _scrape_vrs_with_players(match_date=None, context=None):
    """
    Fetch the VRS rankings page and return a dict of:
        { team_name_lower: { 'pts': float, 'players': [nick, ...] } }
    Used by _find_vrs_team_by_roster for roster-based matching.
    """
    from datetime import timedelta, date as date_cls
    if match_date:
        day_before = match_date - timedelta(days=1)
    else:
        day_before = date_cls.today()

    vrs_url = (f"https://www.hltv.org/valve-ranking/teams/"
               f"{day_before.year}/{day_before.strftime('%B').lower()}/{day_before.day}")

    owns_browser = context is None
    _sess = None
    try:
        if owns_browser:
            _sess = BrowserSession()
            _sess.start()
            context = _sess.context
        page = context.new_page()
        page.goto(vrs_url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_selector(".ranked-team", state="attached", timeout=15000)
        page.wait_for_timeout(2000)

        rankings = page.evaluate("""() => {
            const results = {};
            const entries = document.querySelectorAll('.ranked-team');
            for (const entry of entries) {
                if (entry.innerHTML.indexOf('old-roster') !== -1) continue;
                const ptsEl = entry.querySelector('span.points');
                if (!ptsEl) continue;
                const ptsMatch = ptsEl.textContent.trim().match(/\\((\\d+)\\s*Valve\\s*points\\)/);
                if (!ptsMatch) continue;
                const pts = parseFloat(ptsMatch[1]);
                const fullText = entry.textContent.trim();
                const nameMatch = fullText.match(/#\\d+\\s+([^(]+)\\s*\\(/);
                if (!nameMatch) continue;
                const name = nameMatch[1].trim().toLowerCase();
                // Player nicks live in the always-visible summary row
                // ('.playersLine .rankingNicknames span'). The full lineup
                // table ('.lineup-con .nick') is present in the DOM for every
                // team but stays class="lineup-con hidden" and only gets
                // populated/expanded for whichever single team the page has
                // currently opened — so relying on '.nick' (or any
                // '[class*="nick"]' catch-all, which also matches it) only
                // works for that one team and silently returns nothing for
                // everyone else.
                const playerEls = entry.querySelectorAll('.playersLine .rankingNicknames span');
                const players = Array.from(playerEls)
                    .map(el => el.textContent.trim().toLowerCase())
                    .filter(n => n.length > 0)
                    .slice(0, 5);
                if (name && pts) results[name] = { pts, players };
            }
            return results;
        }""")
        return rankings
    except Exception as e:
        print(f"  [!] VRS roster scrape error: {e}")
        return {}
    finally:
        try:
            if page: page.close()
        except Exception:
            pass
        if owns_browser and _sess:
            _sess.stop()


def _find_vrs_team_by_roster(player_nicks, match_date=None, context=None, min_matches=3):
    """
    When a team can't be found by name in VRS, scrape the VRS rankings page
    and compare player rosters. If a VRS team shares >= min_matches players
    with player_nicks, return (vrs_team_name, vrs_pts). Otherwise None.

    Parameters:
    - player_nicks: list of lowercase player nick strings scraped from the match page
    - match_date:   date object for the match (we use the day-before VRS page)
    - context:      optional BrowserContext to reuse
    - min_matches:  minimum overlapping players to count as a match (default 3)
    """
    if not player_nicks:
        return None

    player_set = set(player_nicks)
    vrs_data = _scrape_vrs_with_players(match_date, context)
    if not vrs_data:
        return None

    best_name = None
    best_pts  = None
    best_count = 0

    for vrs_name, info in vrs_data.items():
        vrs_players = set(info.get('players', []))
        overlap = len(player_set & vrs_players)
        if overlap >= min_matches and overlap > best_count:
            best_count = overlap
            best_name  = vrs_name
            best_pts   = info['pts']

    if best_name is not None:
        return best_name, best_pts
    return None


def auto_register_team(team_name, match_date=None, teams_dict=None, context=None, player_nicks=None):
    """
    Look up VRS points for a brand‑new team and register it.
    The function now ALWAYS prints the raw VRS points that were used,
    no matter which source supplied the value.

    Parameters:
    - context:      optional Playwright BrowserContext to reuse for the VRS lookup
    - player_nicks: optional list of player nick strings scraped from the match page,
                    used as a fallback when name lookup fails (roster matching)
    """
    # ------------------------------------------------------------------
    # 1️⃣  Try the *match‑page forecast* first – this is the most accurate
    # ------------------------------------------------------------------
    if match_date:
        cache_key = str(match_date - timedelta(days=1))
    else:
        cache_key = str(datetime.now().date())

    # Try to reuse a VRS value we already fetched earlier in this run
    if cache_key in _vrs_session_cache and team_name.lower() in _vrs_session_cache[cache_key]:
        vrs = _vrs_session_cache[cache_key][team_name.lower()]
        source = "match page (forecast widget)"
    else:
        # ------------------------------------------------------------------
        # 2️⃣  If the forecast widget is not available, scrape the official VRS
        # ------------------------------------------------------------------
        vrs = scrape_vrs_points(team_name, match_date, context=context)
        source = "official VRS page"

        if vrs is not None:
            _vrs_session_cache.setdefault(cache_key, {})[team_name.lower()] = vrs

    # ------------------------------------------------------------------
    # 3️⃣  Name lookup failed — try roster matching by player nicks
    # ------------------------------------------------------------------
    if vrs is None and player_nicks:
        print(f"  [VRS] Name lookup failed for '{team_name}' — trying roster match ({len(player_nicks)} players)…")
        result = _find_vrs_team_by_roster(player_nicks, match_date, context)
        if result:
            vrs_name, vrs = result
            source = f"roster match (VRS name: '{vrs_name}')"
            print(f"  [VRS] Roster match found: '{vrs_name}' → '{team_name}' ({len(player_nicks)} players checked)")
            _vrs_session_cache.setdefault(cache_key, {})[team_name.lower()] = vrs
        else:
            print(f"  [VRS] Roster match failed for '{team_name}' — no VRS team shares ≥3 players")

    # ------------------------------------------------------------------
    # 4️⃣  No VRS found — register as provisional at 400 CSRS
    # ------------------------------------------------------------------
    if vrs is None:
        csrs = PROVISIONAL_STARTING_RATING
        if teams_dict is not None:
            teams_dict[team_name] = csrs
        provisional_teams[team_name] = 0
        print(
            f"  >>> Provisionally registered '{team_name}': "
            f"no VRS found → {int(csrs)} CSRS (needs {PROVISIONAL_MATCH_THRESHOLD} matches to establish rating)"
        )
        return True

    # ------------------------------------------------------------------
    # 5️⃣  Convert VRS → CSRS and store the new team
    # ------------------------------------------------------------------
    csrs = vrs / 2
    if teams_dict is not None:
        teams_dict[team_name] = csrs

    print(
        f"  >>> Auto‑registered '{team_name}': "
        f"{int(vrs)} VRS ({source}) → {int(csrs)} CSRS Elo"
    )
    return True

def get_imported_urls(history_list):
    """Return a set of all match URLs already in history."""
    return {m.get("url", "") for m in history_list if m.get("url")}


DEBUG_SNAPSHOT_DIR = "debug_snapshots"
DEBUG_SNAPSHOT_MAX_AGE_DAYS = 7  # snapshots older than this are cleaned up automatically


def save_debug_snapshot(page, label: str) -> None:
    """
    Save a screenshot + HTML dump of the current page state for debugging
    a failed/unexpected scrape. Used in scraper except blocks.

    Files are named {label}_{timestamp}.png/.html under DEBUG_SNAPSHOT_DIR.
    Old snapshots beyond DEBUG_SNAPSHOT_MAX_AGE_DAYS are pruned on each call
    so this directory doesn't grow unbounded over long-running automation.
    """
    try:
        os.makedirs(DEBUG_SNAPSHOT_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        base = os.path.join(DEBUG_SNAPSHOT_DIR, f"{safe_label}_{timestamp}")

        try:
            page.screenshot(path=base + ".png", full_page=True, timeout=5000)
        except Exception:
            pass

        try:
            html = page.content()
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        print(f"  [debug] Snapshot saved: {base}.png / .html")
        _prune_debug_snapshots()
    except Exception:
        pass  # debug capture must never break the main flow


def _prune_debug_snapshots(max_age_days: int = DEBUG_SNAPSHOT_MAX_AGE_DAYS) -> None:
    """Remove debug snapshot files older than max_age_days."""
    try:
        if not os.path.isdir(DEBUG_SNAPSHOT_DIR):
            return
        cutoff = time.time() - (max_age_days * 86400)
        for fname in os.listdir(DEBUG_SNAPSHOT_DIR):
            fpath = os.path.join(DEBUG_SNAPSHOT_DIR, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
            except Exception:
                pass
    except Exception:
        pass


def scrape_match_data(url: str, context=None) -> Optional[Tuple[str, str, int, int, str, str, bool, dict, str, dict, Optional[str], Optional[str]]]:
    """
    Scrape teams, scores, date, event, and grand final status from HLTV match page.
    Includes error logging for debugging.
    
    Parameters:
    - url: HLTV match page URL
    - context: optional Playwright BrowserContext to reuse (from BrowserSession).
               If None, launches and tears down its own browser (original standalone behaviour).
    
    Returns:
    - Tuple of (t1_name, t2_name, s1, s2, match_date, event_name, is_grand_final)
    - Returns None if scraping fails
    """
    owns_browser = context is None
    _sess = None

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if owns_browser:
            print("  Starting browser...")
            _sess = BrowserSession()
            _sess.start()
            context = _sess.context

        page = context.new_page()
        
        print("  Connecting to HLTV...")
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        print("  Waiting for page to load...")
        page.wait_for_selector(".teamName", state="attached", timeout=15000)
        page.wait_for_timeout(2000)
        
        data = page.evaluate("""() => {
            const result = {
                teams: [],
                scores: [],
                date: null,
                event: null,
                grand_final: false,
                vrs_before: {}
            };
            
            // === TEAMS ===
            const teamEls = document.querySelectorAll('.teamName');
            if (teamEls.length >= 2) {
                result.teams = Array.from(teamEls).slice(0, 2).map(e => e.textContent.trim());
            }
            
            // === VRS BEFORE (from forecast block on match page) ===
            try {
                const container = document.querySelector('.vrs-forecast-container');
                if (container) {
                    const nameEls = container.querySelectorAll('.vrs-forecast-team-name');
                    const ptEls   = container.querySelectorAll(
                        '.vrs-forecast-left-numbers .vrs-forecast-numbers-wrapper .vrs-forecast-points'
                    );
                    for (let i = 0; i < Math.min(nameEls.length, ptEls.length); i++) {
                        const teamName = nameEls[i].textContent.trim();
                        const ptText   = ptEls[i].textContent.trim().replace('pt', '');
                        const pts      = parseFloat(ptText);
                        if (teamName && !isNaN(pts)) {
                            result.vrs_before[teamName.toLowerCase()] = pts;
                        }
                    }
                }
            } catch(e) {}
            
            
            // === SCORES (FIXED: Handle reversed order) ===
            const scoreDiv = document.querySelector('.score');
            if (scoreDiv) {
                const wonSpan = scoreDiv.querySelector('.won');
                const lostSpan = scoreDiv.querySelector('.lost');
                
                if (wonSpan && lostSpan) {
                    const wonScore = wonSpan.textContent.trim();
                    const lostScore = lostSpan.textContent.trim();
                    
                    // Determine which team is on the left vs right
                    const allSpans = Array.from(scoreDiv.querySelectorAll('span'));
                    const firstSpan = allSpans[0];
                    
                    // If first span is .won, then team1 won
                    // If first span is .lost, then team1 lost
                    if (firstSpan.classList.contains('won')) {
                        result.scores = [wonScore, lostScore];
                    } else if (firstSpan.classList.contains('lost')) {
                        result.scores = [lostScore, wonScore];
                    } else {
                        result.scores = [wonScore, lostScore];
                    }
                } else {
                    const scoreText = scoreDiv.textContent.trim();
                    const scoreParts = scoreText.split(':').map(s => s.trim());
                    if (scoreParts.length >= 2) {
                        result.scores = scoreParts.slice(0, 2);
                    }
                }
            }
            
            // === DATE ===
            const dateEl = document.querySelector('.date[data-unix]') || 
                           document.querySelector('[data-unix]');
            if (dateEl) {
                result.date = {
                    text: dateEl.textContent.trim(),
                    unix: dateEl.getAttribute('data-unix')
                };
            }
            
            // === EVENT ===
            const eventEl = document.querySelector('.event.text-ellipsis a[href*="/events/"]');
            
            if (eventEl) {
                result.event = {
                    text: eventEl.textContent.trim(),
                    href: eventEl.href
                };
            } else {
                const allEventLinks = document.querySelectorAll('a[href*="/events/"]');
                const skipTexts = ['archive', 'events', 'home', 'live', ''];
                
                for (const link of allEventLinks) {
                    const text = link.textContent.trim().toLowerCase();
                    const parentClass = link.parentElement?.className || '';
                    
                    if (skipTexts.includes(text)) {
                        continue;
                    }
                    if (parentClass.includes('underlined')) {
                        continue;
                    }
                    if (link.classList.contains('dropdown-link')) {
                        continue;
                    }
                    
                    result.event = {
                        text: link.textContent.trim(),
                        href: link.href
                    };
                    break;
                }
            }
            
            // === GRAND FINAL / BO1 / FORFEIT ===
            const vetoBox = document.querySelector('.veto-box .preformatted-text');
            let vetoTextRaw = '';
            if (vetoBox) {
                vetoTextRaw = vetoBox.textContent;
                const vetoText = vetoTextRaw.toLowerCase();
                result.grand_final = vetoText.includes('* grand final');
                result.is_bo1 = vetoText.includes('best of 1');
            } else {
                vetoTextRaw = document.body.textContent;
                const bodyText = vetoTextRaw.toLowerCase();
                result.grand_final = bodyText.includes('* grand final');
                result.is_bo1 = bodyText.includes('best of 1');
            }

            // === MATCH STAGE ===
            // Try the dedicated stage element first, then fall back to veto text
            const stageEl = document.querySelector('.matchpage-versus-head-stagename, .stage-name, .match-stage');
            const stageText = stageEl ? stageEl.textContent.trim() : vetoTextRaw;
            const stageLower = stageText.toLowerCase();
            const STAGES = [
                'grand final',
                'upper bracket final',
                'lower bracket final',
                'consolidation final',
                '3rd place decider',
                'upper bracket semi-final',
                'lower bracket semi-final',
                'semi-final',
                'upper bracket quarter-final',
                'lower bracket quarter-final',
                'quarter-final',
            ];
            result.match_stage = null;
            for (const s of STAGES) {
                if (stageLower.includes(s)) {
                    result.match_stage = s.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
                    break;
                }
            }

            // === ENVIRONMENT DETECTION ===
            // HLTV writes "Best of X (LAN)" or "Best of X (Online)" at the start of the veto text
            const vetoLower = vetoTextRaw.toLowerCase();
            if (vetoLower.includes('(lan)')) {
                result.match_env = 'LAN';
            } else if (vetoLower.includes('(online)')) {
                result.match_env = 'ONLINE';
            } else {
                result.match_env = null;
            }

            // === FORFEIT DETECTION ===
            // If the word "forfeit" appears, identify which team forfeited by name match
            result.forfeit_team = null;
            if (vetoTextRaw.toLowerCase().includes('forfeit') && result.teams && result.teams.length === 2) {
                const lowerVeto = vetoTextRaw.toLowerCase();
                const [team1, team2] = result.teams;
                const i1 = lowerVeto.indexOf(team1.toLowerCase());
                const i2 = lowerVeto.indexOf(team2.toLowerCase());
                const forfeitIdx = lowerVeto.indexOf('forfeit');
                // Whichever team name appears closest before the word 'forfeit' is the forfeiting team
                let best = null, bestDist = Infinity;
                if (i1 !== -1 && i1 < forfeitIdx) {
                    const dist = forfeitIdx - i1;
                    if (dist < bestDist) { bestDist = dist; best = 'team1'; }
                }
                if (i2 !== -1 && i2 < forfeitIdx) {
                    const dist = forfeitIdx - i2;
                    if (dist < bestDist) { bestDist = dist; best = 'team2'; }
                }
                result.forfeit_team = best;
            }

            // === BO1 SCORE FIX: Convert round scores to 1-0 map score ===
            // For BO1, HLTV shows rounds (e.g. 16:12) instead of maps (1:0)
            // We compare the two numbers and produce a 1:0 or 0:1 map result
            if (result.is_bo1 && result.scores && result.scores.length === 2) {
                const r1 = parseInt(result.scores[0]);
                const r2 = parseInt(result.scores[1]);
                if (!isNaN(r1) && !isNaN(r2) && r1 !== r2) {
                    result.scores = r1 > r2 ? ['1', '0'] : ['0', '1'];
                }
            }

            // === TEAMS ATTENDING VRS RANKS ===
            const teamBoxes = document.querySelectorAll('.team-box');
            result.event_field = { total_teams: teamBoxes.length, vrs_ranks: [] };
            teamBoxes.forEach(box => {
                const vrsEl = box.querySelector('.event-vrs-rank');
                if (vrsEl) {
                    const rank = parseInt(vrsEl.textContent.trim().replace('#', ''));
                    if (!isNaN(rank)) result.event_field.vrs_ranks.push(rank);
                }
            });

            // === MATCH COMPLETION CHECK ===
            // HLTV shows a .countdown element on the match page. For finished
            // matches it reads "Match over". For live/upcoming matches it shows
            // a live indicator or a countdown timer instead. We use this as the
            // authoritative signal that a match has actually been played —
            // results-listing pages can occasionally include in-progress
            // matches before HLTV finalises them, so this is a second check
            // done on the match page itself (a different page than the results
            // listing) to be certain.
            const countdownEl = document.querySelector('.countdown');
            result.countdown_text = countdownEl ? countdownEl.textContent.trim() : null;
            result.match_over = result.countdown_text
                ? result.countdown_text.toLowerCase().includes('match over')
                : null;  // null = no countdown element found at all (treat cautiously)

            // Also check for the live-match indicator HLTV uses while a match
            // is actively being played
            const liveEl = document.querySelector('.matchpage-live-bar, .live-match-status, .countdown.live');
            result.is_live = !!liveEl;

            // === PLAYER LINEUPS (for roster-based VRS matching) ===
            // Scrape up to 5 player nicks per team from the lineup section.
            // Used as a fallback when a team can't be found by name in VRS.
            try {
                result.lineups = [[], []];
                const lineupBoxes = document.querySelectorAll('.lineup');
                lineupBoxes.forEach((box, i) => {
                    if (i >= 2) return;
                    const nicks = Array.from(box.querySelectorAll('.player-nick, .nick, [class*="nick"]'))
                        .map(el => el.textContent.trim().toLowerCase())
                        .filter(n => n.length > 0)
                        .slice(0, 5);
                    result.lineups[i] = nicks;
                });
            } catch(e) { result.lineups = [[], []]; }

            return result;
        }""")
        
        if not data:
            log_scrape_error("Match", url, "No data extracted from page")
            print_error("No data could be extracted from the page.")
            return None
        
        if len(data.get('teams', [])) < 2:
            log_scrape_error("Match", url, f"Could not find 2 teams. Found: {data.get('teams', [])}")
            print_error("Could not find both teams on the page.")
            return None
        
        if len(data.get('scores', [])) < 2:
            log_scrape_error("Match", url, f"Could not find 2 scores. Found: {data.get('scores', [])}")
            print_error("Could not find match scores on the page.")
            return None

        # === REJECT UNFINISHED MATCHES ===
        # Only "Match over" on the .countdown element confirms the match has
        # actually concluded. is_live or any other countdown text (a running
        # timer, "LIVE", etc.) means the match isn't finished yet — scores
        # shown could still change, so we must not import it.
        countdown_text = data.get('countdown_text')
        match_over     = data.get('match_over')
        is_live        = data.get('is_live', False)

        if is_live or match_over is False:
            reason = f"countdown='{countdown_text}', is_live={is_live}"
            log_scrape_error("Match", url, f"Skipped — match not finished ({reason})")
            _batch_log(f"  [SKIP] Not finished — {url} ({reason})")
            print_error(f"Match not finished yet, skipping ({reason})")
            return None

        if match_over is None:
            # No countdown element found at all — log it but don't block the
            # import outright, since older/archived matches sometimes don't
            # render this element. We just want visibility into when this
            # happens for debugging.
            _batch_log(f"  [WARN] No countdown element found — {url} (importing anyway)")

        t1_name, t2_name = data['teams'][0], data['teams'][1]
        
        try:
            s1 = int(data['scores'][0])
            s2 = int(data['scores'][1])
        except ValueError:
            log_scrape_error("Match", url, f"Invalid score format: {data['scores']}")
            print_error(f"Invalid score format: {data['scores']}")
            return None
        
        match_date = None
        if data.get('date') and data['date'].get('unix'):
            try:
                from datetime import timezone
                unix_ms = int(data['date']['unix'])
                unix_s = unix_ms // 1000
                dt = datetime.fromtimestamp(unix_s, tz=timezone.utc)
                match_date = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception as e:
                log_scrape_error("Match", url, f"Date parse error: {e}")
                match_date = None
        
        event_name = data.get('event', {}).get('text', '') if data.get('event') else ''
        event_href = data.get('event', {}).get('href', '') if data.get('event') else ''
        event_field = data.get('event_field', {})
        is_grand_final = data.get('grand_final', False)
        vrs_before = data.get('vrs_before', {})
        forfeit_team = data.get('forfeit_team', None)  # 'team1', 'team2', or None
        match_env = data.get('match_env', None)  # 'LAN', 'ONLINE', or None
        match_stage = data.get('match_stage') or False  # False = scraped but no stage found; None = never attempted
        lineups = data.get('lineups', [[], []])  # [[t1_player_nicks], [t2_player_nicks]]
        
        print_info(f"Successfully scraped: {t1_name} vs {t2_name}")
        if vrs_before:
            print_info(f"  VRS before: {', '.join(f'{k}: {int(v)}' for k, v in vrs_before.items())}")
        if forfeit_team:
            forfeit_name = t1_name if forfeit_team == 'team1' else t2_name
            print_info(f"  [!] Forfeit detected: {forfeit_name} forfeited the match")

        # Debug log: date/time/event/completion info for every successfully
        # scraped match — makes it easy to audit a batch_import.log afterwards
        # for date ordering issues or matches that shouldn't have been there.
        _batch_log(
            f"  [MATCH] {t1_name} {s1}-{s2} {t2_name} | "
            f"date={match_date} | event='{event_name}' | "
            f"countdown='{countdown_text}' | match_over={match_over} | "
            f"url={url}"
        )

        return t1_name, t2_name, s1, s2, match_date, event_name, is_grand_final, vrs_before, event_href, event_field, forfeit_team, match_env, match_stage, lineups
        
    except PlaywrightTimeout:
        error_msg = f"Page load timeout for URL: {url}"
        log_scrape_error("Match", url, error_msg)
        logger.error(error_msg)
        print_error("HLTV page timed out. This could be due to:")
        print("  - Slow internet connection")
        print("  - HLTV server is busy")
        print("  - URL is invalid or page doesn't exist")
        print("\n  Please check your connection and try again.")
        return None
    except Exception as e:
        error_msg = str(e)
        log_scrape_error("Match", url, error_msg)
        logger.error(f"Scrape error for {url}: {error_msg}")

        if page:
            save_debug_snapshot(page, "match_error")
        
        # Provide specific error messages based on error type
        if "net::ERR" in error_msg:
            print_error("Network error. Please check your internet connection.")
            if "ERR_NAME_NOT_RESOLVED" in error_msg:
                print("  - DNS could not resolve hltv.org")
            elif "ERR_CONNECTION_REFUSED" in error_msg:
                print("  - Connection was refused by HLTV")
            elif "ERR_TIMED_OUT" in error_msg:
                print("  - Connection timed out")
        elif "selector" in error_msg.lower():
            print_error("HLTV page structure may have changed.")
            print("  The scraper couldn't find expected elements on the page.")
            print("  This may require a code update.")
        elif "browser" in error_msg.lower() or "chromium" in error_msg.lower():
            print_error("Browser launch failed.")
            print("  Run 'playwright install' to ensure browsers are installed.")
        elif "permission" in error_msg.lower():
            print_error("Permission denied. Check firewall/antivirus settings.")
        else:
            print_error(f"Unexpected error: {error_msg}")
        
        print("\n  If this persists, please:")
        print("  1. Check your internet connection")
        print("  2. Verify the URL is correct")
        print("  3. Try again in a few minutes")
        print("  4. Check csrs.log for detailed error information")
        return None
    finally:
        try:
            if page: page.close()
        except: pass
        if owns_browser and _sess:
            _sess.stop()

def scrape_event_tier(event_href: str, event_name: str = '', context=None) -> Tuple[str, dict]:
    """
    Visit an HLTV event page and auto-determine tier.

    Parameters:
    - context: optional Playwright BrowserContext to reuse (from BrowserSession).
               If None, launches and tears down its own browser (original standalone behaviour).
    
    Returns:
        (suggested_tier, details_dict) where details_dict contains the counts used.
        suggested_tier is one of: 'S+', 'S', 'A', 'B', 'C', 'D', 'E'
    
    Tier rules:
        S+  : Major AND event page mentions an S-tier venue (Cologne/Krakow) — auto-detected
        S   : IEM Cologne, IEM Krakow, or any Major (by event name) — auto-detected
        A   : >= 6 VRS Top 10 teams  OR  >= 75% of ranked teams are Top 10
        B   : >= 8 VRS Top 20 teams  OR  >= 50% of ranked teams are Top 10
        C   : >= 2 VRS Top 20 teams  OR  >= 25% of ranked teams are Top 20
        D   : >= 3 VRS Top 30 teams  OR  >= 20% of ranked teams are Top 30
        E   : At least 1 ranked team present but below D threshold (catch-all)
    """
    owns_browser = context is None
    _sess = None
    page = None

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if owns_browser:
            print("  Scraping event page for tier detection...")
            _sess = BrowserSession()
            _sess.start()
            context = _sess.context

        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        page.goto(event_href, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        data = page.evaluate("""() => {
            const result = { vrs_ranks: [], total_teams: 0 };
            const teamBoxes = document.querySelectorAll('.team-box');
            result.total_teams = teamBoxes.length;
            teamBoxes.forEach(box => {
                const vrsEl = box.querySelector('.event-vrs-rank');
                if (vrsEl) {
                    const text = vrsEl.textContent.trim().replace('#', '');
                    const rank = parseInt(text);
                    if (!isNaN(rank)) {
                        result.vrs_ranks.push(rank);
                    }
                }
            });
            result.page_text = document.body.textContent.toLowerCase();
            return result;
        }""")

        page_text = data.get('page_text', '')

        vrs_ranks = data.get('vrs_ranks', [])
        total_teams = data.get('total_teams', 0)
        ranked_count = len(vrs_ranks)

        # === S / S+ TIER — name-based, independent of field composition ===
        event_lower = event_name.lower()
        is_major = MAJOR_PATTERN in event_lower
        is_s_tier_name = any(p in event_lower for p in S_TIER_EVENT_NAME_PATTERNS)

        if is_major or is_s_tier_name:
            pct_ranked = (ranked_count / total_teams) if total_teams > 0 else 0.0
            top10 = sum(1 for r in vrs_ranks if r <= 10)
            top20 = sum(1 for r in vrs_ranks if r <= 20)
            top30 = sum(1 for r in vrs_ranks if r <= 30)
            details = {
                'total_teams': total_teams,
                'ranked_count': ranked_count,
                'pct_ranked': pct_ranked,
                'top10': top10, 'top20': top20, 'top30': top30,
                'pct_top10': (top10 / ranked_count) if ranked_count else 0.0,
                'pct_top20': (top20 / ranked_count) if ranked_count else 0.0,
                'pct_top30': (top30 / ranked_count) if ranked_count else 0.0,
                'vrs_ranks': sorted(vrs_ranks),
            }
            if is_major and any(v in page_text for v in S_TIER_VENUE_KEYWORDS):
                return 'S+', details
            return 'S', details

        pct_ranked = (ranked_count / total_teams) if total_teams > 0 else 0.0

        top10 = sum(1 for r in vrs_ranks if r <= 10)
        top20 = sum(1 for r in vrs_ranks if r <= 20)
        top30 = sum(1 for r in vrs_ranks if r <= 30)

        pct_top10 = (top10 / ranked_count) if ranked_count > 0 else 0.0
        pct_top20 = (top20 / ranked_count) if ranked_count > 0 else 0.0
        pct_top30 = (top30 / ranked_count) if ranked_count > 0 else 0.0

        details = {
            'total_teams': total_teams,
            'ranked_count': ranked_count,
            'pct_ranked': pct_ranked,
            'top10': top10,
            'top20': top20,
            'top30': top30,
            'pct_top10': pct_top10,
            'pct_top20': pct_top20,
            'pct_top30': pct_top30,
            'vrs_ranks': sorted(vrs_ranks),
        }

        # Apply tier rules based on VRS field composition.
        # Run on whatever ranked teams are present — E catches small/regional events.
        if top10 >= 6 or pct_top10 >= 0.75:
            tier = 'A'
        elif top20 >= 8 or pct_top10 >= 0.50:
            tier = 'B'
        elif top20 >= 2 or pct_top20 >= 0.25:
            tier = 'C'
        elif top30 >= 3 or pct_top30 >= 0.20:
            tier = 'D'
        else:
            tier = 'E'  # Doesn't meet D — catches all below-D and zero-ranked events

        return tier, details

    except Exception as e:
        log_scrape_error("Event Tier", event_href, str(e))
        print(f"  [!] Event tier scrape failed: {e}")
        if page:
            save_debug_snapshot(page, "event_tier_error")
        return None, {}

    finally:
        try:
            if page: page.close()
        except Exception: pass
        if owns_browser and _sess:
            _sess.stop()


def import_from_hltv(teams_dict: Dict[str, float], history_list: List[Dict[str, Any]], find_team_func, save_func, 
                     update_peak_func=None, event_tiers_dict=None, 
                     calculate_points_func=None, old_roster_check_func=None, context=None) -> None:
    """
    Main import function.

    Parameters:
    - context: optional Playwright BrowserContext to reuse across scrapes
               (from BrowserSession). If None (interactive use),
               each scrape launches and tears down its own browser as before.
    """
    import time
    global total_imports

    while True:
        import_counter = 0
        print_menu(
            "IMPORT FROM HLTV",
            [
                ("1", "Enter HLTV match URL"),
                (None, None),
                ("0", "Back"),
            ],
        )
        
        raw_choice = check_cmd(input("Select: ")).strip()
        if get_cmd(raw_choice) in ['0', 'back']:
            break
        if raw_choice != '1':
            continue

        url_raw = check_cmd(input("Enter HLTV match URL: ")).strip()
        if get_cmd(url_raw) in ['back', '0']:
            continue

        if 'hltv.org/matches/' not in url_raw:
            print("  [!] Invalid URL. Only HLTV match URLs are accepted (hltv.org/matches/...).")
            continue

        imported_urls = get_imported_urls(history_list)
        if url_raw in imported_urls:
            print("  [!] This URL has already been imported. Skipping.")
            continue

        scrape_start = time.time()
        print("  Scraping match data...")
        match_data = scrape_match_data(url_raw, context=context)
        if not match_data:
            print(">>> ERROR: Could not scrape match data. Check the URL and try again.")
            continue

        scrape_time_ms = int((time.time() - scrape_start) * 1000)
        t1_name, t2_name, s1, s2, match_date, event_name, is_grand_final, vrs_before, event_href, event_field, forfeit_team, match_env, match_stage, lineups = match_data

        if not match_date or match_date == 'N/A':
            print("  [WARN] Could not automatically determine match date from page.")
            while True:
                raw_date = input("  Enter match date (YYYY-MM-DD HH:MM) or 'skip' to use current time: ").strip()
                if raw_date.lower() == 'skip':
                    match_date = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
                    print(f"  Using current time as match date: {match_date}")
                    break
                else:
                    try:
                        parsed = datetime.strptime(raw_date, "%Y-%m-%d %H:%M")
                        match_date = parsed.strftime("%Y-%m-%d %H:%M UTC")
                        break
                    except ValueError:
                        try:
                            parsed = datetime.strptime(raw_date, "%Y-%m-%d")
                            match_date = parsed.strftime("%Y-%m-%d 00:00 UTC")
                            break
                        except ValueError:
                            print("  [!] Invalid date format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM")
                            continue

        match_dt = None
        if match_date:
            try:
                dt_str = match_date.replace(' UTC', '')
                match_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            except Exception:
                match_dt = None

        entry_date = str(match_date) if match_date else datetime.now().strftime("%Y-%m-%d %H:%M")

        print(f"\n  Scraped in {scrape_time_ms}ms:")
        if match_date and match_date != 'N/A':
            print(f"  Date: {match_date}")
        if is_grand_final and not match_stage:
            print("  [GRAND FINAL]")
        if match_stage:
            print(f"  [{match_stage}]")
        print(f"  Team 1: {t1_name} ({s1} maps)")
        print(f"  Team 2: {t2_name} ({s2} maps)")
        print(f"  Result: {t1_name} {s1} - {s2} {t2_name}")

        registration_failed = False
        for name in [t1_name, t2_name]:
            if not find_team_func(name):
                if old_roster_check_func is not None:
                    if old_roster_check_func(name):
                        print(f">>> Skipping '{name}' - old roster detected by your system")
                        registration_failed = True
                        break
                
                # Use VRS already scraped from the match page if available
                page_vrs = vrs_before.get(name.lower())
                if page_vrs is not None:
                    csrs = page_vrs / 2
                    teams_dict[name] = csrs
                    print(f"  >>> Auto-registered '{name}': {int(page_vrs)} VRS (match page) -> {int(csrs)} CSRS Elo")
                else:
                    player_nicks = lineups[0] if name == t1_name else lineups[1]
                    ok = auto_register_team(name, match_dt, teams_dict, context=context, player_nicks=player_nicks)
                    if not ok:
                        print(f">>> Skipping match - '{name}' could not be registered.")
                        registration_failed = True
                        break
        
        if registration_failed:
            continue

        t1 = find_team_func(t1_name)
        t2 = find_team_func(t2_name)
        if not t1 or not t2:
            print(">>> ERROR: Teams not found after registration. Skipping match.")
            continue

        VALID_TIERS = ['S+', 'S', 'A', 'B', 'C', 'D', 'E']
        tier_raw = 'A'

        # Extract event slug from match URL — everything after '[team1]-vs-[team2]-'
        # e.g. https://hltv.org/matches/2392712/voca-vs-f5-fragadelphia-york-2026
        # → slug = 'fragadelphia-york-2026'
        event_slug = None
        try:
            url_path = url_raw.rstrip('/').split('/')[-1]  # 'voca-vs-f5-fragadelphia-york-2026'
            vs_idx = url_path.find('-vs-')
            if vs_idx != -1:
                after_vs = url_path[vs_idx + 4:]           # 'f5-fragadelphia-york-2026'
                slug_parts = after_vs.split('-')
                # Skip the first token (team2 name fragment) and take the rest as slug
                # More reliably: find where team2 name ends by checking against t2_name tokens
                t2_slug = t2_name.lower().replace(' ', '-').replace('.', '').replace('_', '-')
                t2_tokens = [tok for tok in t2_slug.split('-') if tok]
                # Walk slug_parts and skip tokens that match t2 name fragments
                skip = 0
                for token in slug_parts:
                    if any(token.lower() in frag or frag in token.lower() for frag in t2_tokens):
                        skip += 1
                    else:
                        break
                event_slug = '-'.join(slug_parts[max(skip, 1):])  # always skip at least 1
        except Exception:
            event_slug = None

        # Lookup: check event_name first, then slug
        stored_tier = None
        if event_tiers_dict:
            if event_name and event_name in event_tiers_dict:
                stored_tier = event_tiers_dict[event_name]
            elif event_slug and event_slug in event_tiers_dict:
                stored_tier = event_tiers_dict[event_slug]

        if stored_tier:
            tier_raw = stored_tier
            print(f"  Event detected: '{event_name}' (using stored tier: {tier_raw})")
        elif event_name:
            print(f"  Event detected: '{event_name}' (new event)")
            # Auto-scrape event page for tier detection
            auto_tier = None
            details = {}
            if event_href and '/events/' in event_href:
                auto_tier, details = scrape_event_tier(event_href, event_name, context=context)
            
            if auto_tier is not None:
                # Show breakdown
                print(f"\n  --- Event Field Analysis ---")
                print(f"  Teams attending : {details.get('total_teams', '?')}")
                print(f"  VRS ranked      : {details.get('ranked_count', '?')} ({details.get('pct_ranked', 0)*100:.0f}%)")
                print(f"  Top 10 VRS      : {details.get('top10', 0)} ({details.get('pct_top10', 0)*100:.0f}%)")
                print(f"  Top 20 VRS      : {details.get('top20', 0)} ({details.get('pct_top20', 0)*100:.0f}%)")
                print(f"  Top 30 VRS      : {details.get('top30', 0)} ({details.get('pct_top30', 0)*100:.0f}%)")
                print(f"\n  Auto-detected tier: {auto_tier}")
                if auto_tier in ('S+', 'S'):
                    print(f"  (Detected via event name{'/venue' if auto_tier == 'S+' else ''}: '{event_name}')")
                tier_raw = auto_tier
            else:
                # Scrape failed entirely — fall back to manual
                print(f"  [!] Could not auto-detect tier. Please enter manually.")
                raw = check_cmd(input(f"  Tier ({'/'.join(VALID_TIERS)}) or 'skip' for default (A): ")).strip().upper()
                tier_raw = raw if raw in VALID_TIERS else 'A'

            if tier_raw and event_name:
                event_tiers_dict[event_name] = tier_raw
                print(f"  Saved tier '{tier_raw}' for {event_name}.")
        else:
            raw = check_cmd(input(f"  Tier ({'/'.join(VALID_TIERS)}) or 'skip' for default (A): ")).strip().upper()
            tier_raw = raw if raw in VALID_TIERS else 'A'
        
        # Auto-detect environment from veto box text "(LAN)" / "(Online)"
        env_map = {'1': 'ONLINE', '2': 'LAN'}
        if match_env in ('ONLINE', 'LAN'):
            env_auto = match_env
        else:
            env_auto = None

        if env_auto:
            print(f"  Environment auto-detected: {env_auto} (from '{env_auto}' tag)")
            env_raw = env_auto
        else:
            print("  [!] Could not auto-detect environment (no LAN/Online tag found).")
            print("  Environment: 1. Online  2. LAN  0. Back")
            env_choice = check_cmd(input("  Select: ")).strip()
            if get_cmd(env_choice) in ['0', 'back']:
                continue
            env_raw = env_map.get(env_choice, 'LAN')

        is_gf = is_grand_final

        # Build depreciated baseline — use match_dt for precise same-day ordering.
        # This must use the match's own date as the depreciation reference point,
        # NOT real wall-clock "now" — otherwise, during a backfill of historical
        # matches, every team gets depreciated based on (today - their last
        # historical match date), which is a huge, almost-uniform gap unrelated
        # to the actual point-in-time standings being reconstructed. That made
        # this ranking snapshot diverge from the actual stored ratings (which
        # correctly depreciate relative to match_dt via apply_depreciation_to_rating
        # below), producing rank orderings that contradicted the printed point
        # totals.
        as_of_date = match_dt.date() if match_dt else datetime.now().date()
        dep_base = {}
        date_index = build_match_date_index(history)
        for name, pts in teams_dict.items():
            if is_provisional(name):
                continue
            last = get_team_last_match_date_before(name, before_date=match_dt, index=date_index) if match_dt else get_team_last_match_date_before(name, index=date_index)
            if last:
                d = (as_of_date - last.date()).days
                dep_base[name] = calculate_depreciation(pts, d, name) if d > DEPRECIATION_THRESHOLD else pts
            else:
                dep_base[name] = pts

        old_order = sorted(dep_base.keys(), key=lambda x: dep_base[x], reverse=True)
        
        p1_before = teams_dict.get(t1, 1000)
        p2_before = teams_dict.get(t2, 1000)

        # Apply depreciation before calculating points (matches resimulate behaviour)
        if match_dt is not None:
            p1_before = apply_depreciation_to_rating(t1, p1_before, match_dt)
            p2_before = apply_depreciation_to_rating(t2, p2_before, match_dt)

        new_p1, new_p2 = p1_before, p2_before

        # Determine forfeit info
        forfeiting_team = None
        if forfeit_team == 'team1':
            forfeiting_team = t1
        elif forfeit_team == 'team2':
            forfeiting_team = t2

        if forfeiting_team:
            print(f"  [!] Forfeit: {forfeiting_team} forfeited — only their rating will change.")

        if calculate_points_func is not None:
            form1 = calculate_form(t1, n=15, history=history_list)
            form2 = calculate_form(t2, n=15, history=history_list)
            form_adj_1 = (form1[1] - 50) if form1 else 0
            form_adj_2 = (form2[1] - 50) if form2 else 0

            t1_prov = is_provisional(t1)
            t2_prov = is_provisional(t2)
            t1_k = get_provisional_k(t1) if t1_prov else 1.0
            t2_k = get_provisional_k(t2) if t2_prov else 1.0

            # Show provisional notice
            if t1_prov:
                matches_done = provisional_teams.get(t1, 0)
                print(f"  [Provisional] {t1}: match {matches_done + 1}/{PROVISIONAL_MATCH_THRESHOLD} (K×{t1_k})")
            if t2_prov:
                matches_done = provisional_teams.get(t2, 0)
                print(f"  [Provisional] {t2}: match {matches_done + 1}/{PROVISIONAL_MATCH_THRESHOLD} (K×{t2_k})")

            if forfeiting_team:
                # Only the forfeiting team's rating changes — as a loss.
                # The non-forfeiting team did nothing to earn a rating change.
                if forfeiting_team == t1:
                    raw_p1 = calculate_points_func(p1_before, p2_before, 0, abs(s1 - s2) or 1, tier_raw, env_raw, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
                    raw_p2 = p2_before
                else:
                    raw_p1 = p1_before
                    raw_p2 = calculate_points_func(p2_before, p1_before, 0, abs(s1 - s2) or 1, tier_raw, env_raw, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)
            else:
                raw_p1 = calculate_points_func(p1_before, p2_before, 1 if s1 > s2 else 0, abs(s1 - s2), tier_raw, env_raw, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
                raw_p2 = calculate_points_func(p2_before, p1_before, 1 if s2 > s1 else 0, abs(s1 - s2), tier_raw, env_raw, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)

            # Apply provisional K multiplier — only affects the provisional team's own change
            if t1_prov:
                change1 = raw_p1 - p1_before
                new_p1 = min(max(RATING_FLOOR, p1_before + change1 * t1_k), RATING_CAP)
            else:
                new_p1 = raw_p1
            if t2_prov:
                change2 = raw_p2 - p2_before
                new_p2 = min(max(RATING_FLOOR, p2_before + change2 * t2_k), RATING_CAP)
            else:
                new_p2 = raw_p2

            teams_dict[t1] = new_p1
            teams_dict[t2] = new_p2

            # Increment provisional match counters (may graduate teams)
            # In a forfeit, only the forfeiting team is considered to have "played"
            if t1_prov and (not forfeiting_team or forfeiting_team == t1):
                increment_provisional(t1)
            if t2_prov and (not forfeiting_team or forfeiting_team == t2):
                increment_provisional(t2)
        
        t1_rank_shift = 0
        t2_rank_shift = 0
        
        if calculate_points_func is not None:
            # See matching fix/comment in the batch-import path: dep_base only
            # contains non-provisional teams, so dep_new must apply that same
            # rule to t1/t2 instead of force-inserting them unconditionally —
            # otherwise a still-provisional team gets ranked against the full
            # established pool while other provisional teams stay excluded,
            # and old_order.index(t1) raises ValueError outright when t1 was
            # provisional before this match.
            dep_new = dict(dep_base)
            if t1 in dep_new or not is_provisional(t1):
                dep_new[t1] = new_p1
            if t2 in dep_new or not is_provisional(t2):
                dep_new[t2] = new_p2
            new_order = sorted(dep_new.keys(), key=lambda x: dep_new[x], reverse=True)

            if t1 in old_order and t1 in new_order:
                t1_rank_shift = (old_order.index(t1) + 1) - (new_order.index(t1) + 1)
            if t2 in old_order and t2 in new_order:
                t2_rank_shift = (old_order.index(t2) + 1) - (new_order.index(t2) + 1)

            for name, pts_before, pts_after, rank_shift in [
                (t1, p1_before, new_p1, t1_rank_shift),
                (t2, p2_before, new_p2, t2_rank_shift)
            ]:
                pts_sign = "+" if pts_after >= pts_before else ""
                if rank_shift > 0:
                    rank_sign = f"+{rank_shift} ↑"
                elif rank_shift < 0:
                    rank_sign = f"{rank_shift} ↓"
                else:
                    rank_sign = "─"
                rank_label = f"#{new_order.index(name) + 1}" if name in new_order else "provisional (unranked)"
                print(f"  {name}: {int(pts_before)} -> {int(pts_after)} ({pts_sign}{int(pts_after - pts_before)}) | Rank: {rank_label} ({rank_sign})")

        entry = {
            "date": entry_date,
            "tier": tier_raw,
            "env": env_raw,
            "grand_final": is_gf,
            "match_stage": match_stage,
            "source": "HLTV Import",
            "url": url_raw,
            "t1": {"name": t1, "score": s1, "pts_before": p1_before, "pts_after": new_p1, "rank_shift": t1_rank_shift},
            "t2": {"name": t2, "score": s2, "pts_before": p2_before, "pts_after": new_p2, "rank_shift": t2_rank_shift},
        }
        if event_name:
            entry["event"] = event_name
        if forfeiting_team:
            entry["forfeit"] = forfeiting_team

        is_valid, error_msg = validate_match_entry(entry)
        if not is_valid:
            print(f"  [!] Invalid match data: {error_msg}")
            print("  Skipping this match. Please report this bug.")
            continue

        history_list.append(entry)
        
        if update_peak_func:
            update_peak_func(t1, teams_dict.get(t1, 1000), entry_date)
            update_peak_func(t2, teams_dict.get(t2, 1000), entry_date)

        mark_unsaved()
        total_imports += 1
        import_counter += 1
        if import_counter % 5 == 0:
            save_all(silent=True)

    _vrs_session_cache.clear()

BATCH_LOG_FILE = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "logs", "normal", "batch_import.log")

def _batch_log(msg: str) -> None:
    """Write a timestamped line to the batch import log and stdout."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(BATCH_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


HLTV_RESULTS_PAGE_SIZE = 100   # confirmed live: HLTV's own "1 - 100 of N" counter on /results
HLTV_RESULTS_MAX_PAGES = 500   # circuit breaker (50,000 matches) in case the total-count parse ever fails

# ── Month/date parsing helpers ────────────────────────────────────────────────

_MONTH_MAP = {
    'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
    'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12,
    'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
    'July':7,'August':8,'September':9,'October':10,'November':11,'December':12,
}

def _parse_hltv_end_date(date_str: str, year: int = 2026) -> str | None:
    """
    Parse HLTV date range strings like 'Jun 11th-Jun 21st' or 'Jun 25th-Jun 26th'
    and return the END date as 'YYYY-MM-DD'.
    """
    parts = date_str.strip().split('-')
    end_part = parts[-1].strip() if len(parts) > 1 else parts[0].strip()
    m = re.match(r'(\w+)\s*(\d+)', end_part)
    if m:
        month = _MONTH_MAP.get(m.group(1), 0)
        day   = int(re.sub(r'\D', '', m.group(2)))
        if month:
            return f"{year}-{month:02d}-{day:02d}"
    return None

def _parse_hltv_month_year(text: str):
    """Parse 'June 20261 - 50 of 8019' -> (2026, 6)"""
    m = re.match(r'(\w+)\s+(\d{4})', text.strip())
    if m:
        month = _MONTH_MAP.get(m.group(1), 0)
        year  = int(m.group(2))
        return year, month
    return None, None


def scrape_events_archive(
    start_date: str,
    cookies: list | None = None,
    event_index_file: str = os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "data", "event_index.json"),
) -> list[dict]:
    """
    Walk /events/archive?offset=0,50,100,... using Camoufox + real cookies,
    parsing event IDs and end dates. Stops when all events on a page are older
    than start_date. Caches results in event_index_file so repeat calls are fast.

    Returns list of dicts: [{id, name, end_date}, ...]
    """
    import json as _json
    from bs4 import BeautifulSoup

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()

    # Load existing index
    index: dict[str, dict] = {}
    if os.path.exists(event_index_file):
        try:
            with open(event_index_file) as f:
                index = {str(e["id"]): e for e in _json.load(f)}
        except Exception:
            index = {}

    if not cookies:
        cookies = _load_hltv_cookies()

    pw_cookies = [
        {"name": c.get("name",""), "value": c.get("value",""),
         "domain": c.get("domain",".hltv.org"), "path": c.get("path","/")}
        for c in cookies if c.get("name") and c.get("value")
    ]

    newly_found = []
    offset = 0
    stop_early = False

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        _batch_log("  [ERROR] Camoufox not installed — cannot scrape events archive")
        return list(index.values())

    with Camoufox(headless=True, os=("windows","macos","linux")) as browser:
        context = browser.new_context()
        if pw_cookies:
            context.add_cookies(pw_cookies)
        page = context.new_page()

        # Warm up the session via homepage first — CF is much more likely to
        # pass subsequent requests after seeing a normal landing page visit
        _batch_log("  Warming up session via hltv.org homepage...")
        try:
            page.goto("https://www.hltv.org", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        except Exception:
            pass

        while not stop_early:
            url = f"https://www.hltv.org/events/archive?offset={offset}"
            _batch_log(f"  Scraping events archive offset={offset}...")
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                html = page.content()
            except Exception as e:
                _batch_log(f"  [WARN] Archive page failed at offset={offset}: {e}")
                break

            if "Just a moment" in html or "challenge-platform" in html:
                _batch_log("  [WARN] Cloudflare challenge on archive page — stopping")
                break

            soup = BeautifulSoup(html, "html.parser")
            month_blocks = soup.find_all("div", class_="events-month")

            if not month_blocks:
                _batch_log("  No events-month blocks found — stopping")
                break

            page_had_events = False
            for block in month_blocks:
                headline = block.find(class_="standard-headline")
                if not headline:
                    continue
                year, month_num = _parse_hltv_month_year(headline.get_text(strip=True))
                if not year:
                    continue

                for event_link in block.find_all("a", href=re.compile(r'/events/\d+/')):
                    href = event_link.get("href", "")
                    id_m = re.search(r'/events/(\d+)/', href)
                    if not id_m:
                        continue
                    event_id = int(id_m.group(1))
                    page_had_events = True

                    # Already indexed
                    if str(event_id) in index:
                        evt = index[str(event_id)]
                        if evt.get("end_date") and evt["end_date"] < start_date:
                            stop_early = True
                        continue

                    name_el = event_link.find(class_="text-ellipsis")
                    name = name_el.get_text(strip=True) if name_el else href

                    # Parse end date from col-desc
                    end_date = None
                    for d in event_link.find_all(class_="col-desc"):
                        txt = d.get_text(strip=True)
                        if re.search(r'\w{3}\s*\d+.*-.*\w{3}\s*\d+', txt):
                            end_date = _parse_hltv_end_date(txt, year)
                            break

                    evt = {"id": event_id, "name": name, "end_date": end_date}
                    index[str(event_id)] = evt
                    newly_found.append(evt)

                    # Stop if this event ended before our target start
                    if end_date and end_date < start_date:
                        stop_early = True

            if not page_had_events:
                break

            offset += 50
            import time as _time; _time.sleep(2)

        page.close()

    # Save updated index
    try:
        with open(event_index_file, "w") as f:
            _json.dump(list(index.values()), f, indent=2)
        _batch_log(f"  Event index saved: {len(index)} total events")
    except Exception as e:
        _batch_log(f"  [WARN] Could not save event index: {e}")

    # Return all events ending on or after start_date
    result = [
        e for e in index.values()
        if e.get("end_date") and e["end_date"] >= start_date
    ]
    if result:
        dates = sorted(e["end_date"] for e in result)
        _batch_log(
            f"  [EVENT INDEX] {len(result)} events in range | "
            f"earliest_end={dates[0]} | latest_end={dates[-1]}"
        )
    return result


def scrape_active_events(cookies: list | None = None) -> list[dict]:
    """
    Scrape /events to get currently active/upcoming event IDs.
    Returns list of dicts: [{id, name}, ...]
    """
    import json as _json
    from bs4 import BeautifulSoup

    if not cookies:
        cookies = _load_hltv_cookies()

    pw_cookies = [
        {"name": c.get("name",""), "value": c.get("value",""),
         "domain": c.get("domain",".hltv.org"), "path": c.get("path","/")}
        for c in cookies if c.get("name") and c.get("value")
    ]

    events = []
    try:
        from camoufox.sync_api import Camoufox
        with Camoufox(headless=True, os=("windows","macos","linux")) as browser:
            context = browser.new_context()
            if pw_cookies:
                context.add_cookies(pw_cookies)
            page = context.new_page()
            # Warm up session
            try:
                page.goto("https://www.hltv.org", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
            except Exception:
                pass
            page.goto("https://www.hltv.org/events", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            html = page.content()
            page.close()

        if "Just a moment" in html or "challenge-platform" in html:
            _batch_log("  [WARN] CF challenge on /events — no active events scraped")
            return events

        soup = BeautifulSoup(html, "html.parser")
        for event_link in soup.find_all("a", href=re.compile(r'/events/\d+/')):
            href = event_link.get("href","")
            id_m = re.search(r'/events/(\d+)/', href)
            if not id_m:
                continue
            event_id = int(id_m.group(1))
            name_el = event_link.find(class_="text-ellipsis")
            name = name_el.get_text(strip=True) if name_el else href
            if not any(e["id"] == event_id for e in events):
                events.append({"id": event_id, "name": name})

    except ImportError:
        _batch_log("  [WARN] Camoufox not installed — cannot scrape active events")
    except Exception as e:
        _batch_log(f"  [WARN] Active events scrape failed: {e}")

    return events


def scrape_hltv_results_by_event(event_id: int, cookies: list | None = None, page=None) -> list[str]:
    """
    Scrape /results?event=<id> for all match URLs for a given event.
    Uses Camoufox (which bypasses CF with real cookies) for the listing page.
    Returns de-duplicated list of full match URLs, oldest-first.

    Pass an existing Camoufox page object to reuse a browser session across
    multiple events (avoids repeated browser launches and warm-ups).
    """
    import time as _time, random as _random
    from bs4 import BeautifulSoup

    if not cookies:
        cookies = _load_hltv_cookies()

    pw_cookies = [
        {"name": c.get("name",""), "value": c.get("value",""),
         "domain": c.get("domain",".hltv.org"), "path": c.get("path","/")}
        for c in cookies if c.get("name") and c.get("value")
    ]

    all_urls: list[str] = []
    seen: set[str] = set()

    owns_browser = page is None

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        _batch_log("  [ERROR] Camoufox not installed.")
        return all_urls

    base_url = f"https://www.hltv.org/results?event={event_id}"
    offset = 0
    total  = None

    try:
        if owns_browser:
            _browser_ctx = Camoufox(headless=_CAMOUFOX_HEADLESS, os=("windows","macos","linux"))
            browser = _browser_ctx.__enter__()
            context = browser.new_context()
            if pw_cookies:
                context.add_cookies(pw_cookies)
            page = context.new_page()
            # Warm up via homepage
            try:
                page.goto("https://www.hltv.org", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception:
                pass

        while True:
            url = base_url if offset == 0 else f"{base_url}&offset={offset}"
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception as e:
                _batch_log(f"  [WARN] Event {event_id} page failed at offset {offset}: {e}")
                break

            html = page.content()

            if "Just a moment" in html or "challenge-platform" in html:
                _batch_log(f"  [WARN] CF challenge on event {event_id} results — stopping")
                break

            soup = BeautifulSoup(html, "html.parser")

            if total is None:
                body_text = soup.get_text()
                m = re.search(r'(\d+)\s*-\s*(\d+)\s*of\s*(\d+)', body_text)
                if m:
                    total = int(m.group(3))

            # IMPORTANT: HLTV's /results page (including ?event=<id>) also
            # renders a "RECENT ACTIVITY" sidebar widget further down the
            # page that links to /matches/<id>/... URLs for unrelated,
            # recently-played matches — not scoped to this event at all.
            # Searching the whole page picks those up too, which is what
            # caused unrelated events' matches to bleed into an event's
            # scraped URL list. Scoping to div.results excludes the sidebar.
            results_container = soup.find("div", class_="results")
            search_root = results_container if results_container is not None else soup
            if results_container is None:
                _batch_log(f"  [WARN] event {event_id}: could not find div.results — "
                           f"falling back to whole-page search, results may include sidebar links")

            hrefs = []
            for a in search_root.find_all("a", href=re.compile(r'^/matches/\d+/')):
                full_url = "https://www.hltv.org" + a.get("href","")
                if full_url not in seen:
                    seen.add(full_url)
                    all_urls.append(full_url)
                    hrefs.append(full_url)

            if not hrefs:
                break
            if total is not None and offset + HLTV_RESULTS_PAGE_SIZE >= total:
                break

            offset += HLTV_RESULTS_PAGE_SIZE
            _time.sleep(_random.uniform(1.0, 2.5))

    finally:
        if owns_browser:
            try: page.close()
            except: pass
            try: _browser_ctx.__exit__(None, None, None)
            except: pass

    # HLTV results pages are newest-first. Reverse so we import
    # chronologically oldest-first — critical for correct Elo calculation.
    ordered = list(reversed(all_urls))

    _batch_log(
        f"  [EVENT SCRAPE] event_id={event_id} | "
        f"total_urls={len(ordered)} | "
        f"first={ordered[0] if ordered else None} | "
        f"last={ordered[-1] if ordered else None}"
    )

    return ordered


def scrape_hltv_results(start_date: str, end_date: str = None, context=None) -> list[str]:
    """
    DEPRECATED date-range scraper — kept for backward compatibility.
    Now routes through event-based scraping internally.

    For the daemon's rolling window, we scrape active events instead of
    date-range URLs (which are Cloudflare-blocked).
    """
    _batch_log("  [INFO] Using event-based scraping (date-range URLs are CF-blocked)")
    cookies = _load_hltv_cookies()

    # Get active events from /events page
    active = scrape_active_events(cookies=cookies)
    if not active:
        _batch_log("  [WARN] No active events found — nothing to scrape")
        return []

    _batch_log(f"  Found {len(active)} active events to check")
    all_urls: list[str] = []
    seen: set[str] = set()

    for evt in active:
        _batch_log(f"  Scraping event {evt['id']}: {evt['name'][:50]}")
        urls = scrape_hltv_results_by_event(evt["id"], cookies=cookies)
        for u in urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
        _batch_log(f"    -> {len(urls)} match URLs")

    return all_urls


    """
    Walk HLTV's /results listing for every match URL between start_date and
    end_date (inclusive), across every tier HLTV lists.

    Uses curl_cffi to impersonate a real browser's TLS fingerprint, bypassing
    Cloudflare's JS challenge which blocks Playwright/Camoufox on the results
    listing page. Individual match pages are still scraped via BrowserSession.

    Returns a de-duplicated, order-preserved list of full match URLs.
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    all_urls: List[str] = []
    seen = set()

    # ── curl_cffi: impersonates Chrome TLS fingerprint, bypasses CF ──
    try:
        from curl_cffi import requests as cf_requests  # type: ignore
        from bs4 import BeautifulSoup                  # type: ignore
    except ImportError:
        _batch_log("  [ERROR] curl_cffi or beautifulsoup4 not installed. Run: pip install curl_cffi beautifulsoup4")
        return all_urls

    session = cf_requests.Session(impersonate="chrome136")
    session.headers.update({
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Cache-Control":             "max-age=0",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   "https://www.google.com/",
    })
    # Add HLTV cookies if available
    cookies = _load_hltv_cookies()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ".hltv.org"))
    if not cookies:
        session.cookies.set("cookieConsent", "1", domain=".hltv.org")
        session.cookies.set("hltvConsent",   "true", domain=".hltv.org")

    offset = 0
    total  = None
    _batch_log(f"  Scraping HLTV results {start_date} -> {end_date} ...")

    import re, time, random

    while True:
        if offset // HLTV_RESULTS_PAGE_SIZE >= HLTV_RESULTS_MAX_PAGES:
            _batch_log(f"  [WARN] Hit page safety cap ({HLTV_RESULTS_MAX_PAGES} pages) — stopping.")
            break

        list_url = f"https://www.hltv.org/results?startDate={start_date}&endDate={end_date}&offset={offset}"
        try:
            resp = session.get(list_url, timeout=30)
            if resp.status_code == 403:
                # Retry once with a different impersonate target
                _batch_log(f"  [WARN] 403 with chrome136, retrying with chrome131...")
                session2 = cf_requests.Session(impersonate="chrome131")
                session2.headers.update(session.headers)
                session2.cookies.update(session.cookies)
                resp = session2.get(list_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            _batch_log(f"  [WARN] Request failed at offset {offset}: {e}")
            break

        html = resp.text

        # Check if still getting a CF challenge
        if "Just a moment" in html or "challenge-platform" in html:
            _batch_log(f"  [WARN] Still getting Cloudflare challenge — try adding hltv_cookies.json")
            break

        soup = BeautifulSoup(html, "html.parser")

        # Parse total from "1 - 100 of 3482"
        if total is None:
            body_text = soup.get_text()
            m = re.search(r'(\d+)\s*-\s*(\d+)\s*of\s*(\d+)', body_text)
            if m:
                total = int(m.group(3))
                _batch_log(f"  HLTV reports {total} matches in this range.")

        hrefs = []
        for a in soup.find_all("a", href=re.compile(r'^/matches/\d+/')):
            href = a.get("href", "")
            full_url = "https://www.hltv.org" + href
            if full_url not in seen:
                seen.add(full_url)
                all_urls.append(full_url)
                hrefs.append(href)

        _batch_log(f"  Offset {offset}: {len(hrefs)} new URLs (collected: {len(all_urls)})")

        if not hrefs:
            break
        if total is not None and offset + HLTV_RESULTS_PAGE_SIZE >= total:
            break

        offset += HLTV_RESULTS_PAGE_SIZE
        time.sleep(random.uniform(1.0, 2.5))  # polite delay between pages

    return all_urls



def _import_url_list(urls_to_do: List[str], ctx) -> Tuple[int, List[str]]:
    """
    Shared core of the batch importer: takes a list of HLTV match URLs and a
    live Playwright context, imports every one of them with the same logic
    as the interactive importer (tier detection, provisional system, forfeit
    handling), and returns (import_counter, failed_urls).

    Pulled out of run_batch_import() so both the file-based flow and the
    auto-backfill-by-date-range flow can share one import loop.
    """
    VALID_TIERS = ['S+', 'S', 'A', 'B', 'C', 'D', 'E']
    global total_imports
    import_counter = 0
    failed = []

    for idx, url_raw in enumerate(urls_to_do, 1):
        _batch_log(f"[{idx}/{len(urls_to_do)}] {url_raw}")

        # --- Scrape ---
        match_data = scrape_match_data(url_raw, context=ctx)
        if not match_data:
            _batch_log(f"  FAIL: could not scrape match data")
            failed.append(url_raw)
            continue

        t1_name, t2_name, s1, s2, match_date, event_name, is_grand_final, vrs_before, event_href, event_field, forfeit_team, match_env, match_stage, lineups = match_data

        # --- Date ---
        if not match_date or match_date == 'N/A':
            match_date = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
            _batch_log(f"  [WARN] No date on page — using current time: {match_date}")

        match_dt = None
        try:
            match_dt = datetime.strptime(match_date.replace(' UTC', ''), "%Y-%m-%d %H:%M")
        except Exception:
            pass

        entry_date = str(match_date) if match_date else datetime.now().strftime("%Y-%m-%d %H:%M")
        _batch_log(f"  {t1_name} {s1} - {s2} {t2_name}  |  {match_date}  |  {event_name or 'Unknown event'}")

        # --- Team registration ---
        registration_failed = False
        for name in [t1_name, t2_name]:
            if not find_team(name):
                page_vrs = vrs_before.get(name.lower())
                if page_vrs is not None:
                    csrs = page_vrs / 2
                    teams[name] = csrs
                    _batch_log(f"  Auto-registered '{name}': {int(page_vrs)} VRS -> {int(csrs)} CSRS")
                else:
                    player_nicks = lineups[0] if name == t1_name else lineups[1]
                    ok = auto_register_team(name, match_dt, teams, context=ctx, player_nicks=player_nicks)
                    if not ok:
                        _batch_log(f"  FAIL: could not register '{name}' — skipping match")
                        registration_failed = True
                        break

        if registration_failed:
            failed.append(url_raw)
            continue

        t1 = find_team(t1_name)
        t2 = find_team(t2_name)
        if not t1 or not t2:
            _batch_log(f"  FAIL: teams not found after registration")
            failed.append(url_raw)
            continue

        # --- Tier ---
        tier_raw = 'A'
        event_slug = None
        try:
            url_path = url_raw.rstrip('/').split('/')[-1]
            vs_idx = url_path.find('-vs-')
            if vs_idx != -1:
                after_vs = url_path[vs_idx + 4:]
                slug_parts = after_vs.split('-')
                t2_slug = t2_name.lower().replace(' ', '-').replace('.', '').replace('_', '-')
                t2_tokens = [tok for tok in t2_slug.split('-') if tok]
                skip = 0
                for token in slug_parts:
                    if any(token.lower() in frag or frag in token.lower() for frag in t2_tokens):
                        skip += 1
                    else:
                        break
                event_slug = '-'.join(slug_parts[max(skip, 1):])
        except Exception:
            event_slug = None

        stored_tier = None
        if event_name and event_name in event_tiers:
            stored_tier = event_tiers[event_name]
        elif event_slug and event_slug in event_tiers:
            stored_tier = event_tiers[event_slug]

        if stored_tier:
            tier_raw = stored_tier
            _batch_log(f"  Tier: {tier_raw} (from stored event_tiers)")
        elif event_href and '/events/' in event_href:
            auto_tier, details = scrape_event_tier(event_href, event_name or '', context=ctx)
            if auto_tier is not None:
                tier_raw = auto_tier
                _batch_log(f"  Tier: {tier_raw} (auto-detected from event page)")
                if event_name:
                    event_tiers[event_name] = tier_raw
                    mark_unsaved()
            else:
                # Scrape failed entirely (exception/timeout) — fall back to E
                tier_raw = 'E'
                _batch_log(f"  Tier: E (scrape failed, using fallback)")
        else:
            tier_raw = 'E'
            _batch_log(f"  Tier: E (no event href, using fallback)")

        # --- Environment ---
        if match_env in ('ONLINE', 'LAN'):
            env_raw = match_env
            _batch_log(f"  Env: {env_raw} (auto-detected)")
        else:
            env_raw = 'LAN'
            _batch_log(f"  Env: LAN (default — no tag found)")

        is_gf = is_grand_final
        if is_gf and not match_stage:
            _batch_log(f"  [GRAND FINAL]")
        if match_stage:
            _batch_log(f"  [{match_stage}]")

        # --- Forfeit ---
        forfeiting_team = None
        if forfeit_team == 'team1':
            forfeiting_team = t1
        elif forfeit_team == 'team2':
            forfeiting_team = t2
        if forfeiting_team:
            _batch_log(f"  [Forfeit] {forfeiting_team}")

        # --- Depreciation baseline ---
        # Use the match's own date as the depreciation reference point, not
        # real wall-clock "now" — see matching comment/fix in the interactive
        # import path above. Using real "now" here during a backfill made the
        # rank-ordering snapshot diverge wildly from the actual point totals
        # (e.g. a team correctly at 852 pts showing a worse rank than a team
        # at 839 pts), since it depreciated every team by ~(today - their
        # historical last-match date) instead of relative to match_dt.
        as_of_date = match_dt.date() if match_dt else datetime.now().date()
        dep_base = {}
        date_index = build_match_date_index(history)
        for name, pts in teams.items():
            if is_provisional(name):
                continue
            last = get_team_last_match_date_before(name, before_date=match_dt, index=date_index) if match_dt else get_team_last_match_date_before(name, index=date_index)
            if last:
                d = (as_of_date - last.date()).days
                dep_base[name] = calculate_depreciation(pts, d, name) if d > DEPRECIATION_THRESHOLD else pts
            else:
                dep_base[name] = pts

        old_order = sorted(dep_base.keys(), key=lambda x: dep_base[x], reverse=True)

        p1_before = teams.get(t1, 1000)
        p2_before = teams.get(t2, 1000)
        if match_dt is not None:
            p1_before = apply_depreciation_to_rating(t1, p1_before, match_dt)
            p2_before = apply_depreciation_to_rating(t2, p2_before, match_dt)

        # --- Calculate points ---
        form1 = calculate_form(t1, n=15, history=history)
        form2 = calculate_form(t2, n=15, history=history)
        form_adj_1 = (form1[1] - 50) if form1 else 0
        form_adj_2 = (form2[1] - 50) if form2 else 0

        t1_prov = is_provisional(t1)
        t2_prov = is_provisional(t2)
        t1_k = get_provisional_k(t1) if t1_prov else 1.0
        t2_k = get_provisional_k(t2) if t2_prov else 1.0

        if forfeiting_team:
            if forfeiting_team == t1:
                raw_p1 = calculate_points(p1_before, p2_before, 0, abs(s1 - s2) or 1, tier_raw, env_raw, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
                raw_p2 = p2_before
            else:
                raw_p1 = p1_before
                raw_p2 = calculate_points(p2_before, p1_before, 0, abs(s1 - s2) or 1, tier_raw, env_raw, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)
        else:
            raw_p1 = calculate_points(p1_before, p2_before, 1 if s1 > s2 else 0, abs(s1 - s2), tier_raw, env_raw, is_gf, form_adj_1, form_adj_2, opp_is_provisional=t2_prov)
            raw_p2 = calculate_points(p2_before, p1_before, 1 if s2 > s1 else 0, abs(s1 - s2), tier_raw, env_raw, is_gf, form_adj_2, form_adj_1, opp_is_provisional=t1_prov)

        if t1_prov:
            new_p1 = min(max(RATING_FLOOR, p1_before + (raw_p1 - p1_before) * t1_k), RATING_CAP)
        else:
            new_p1 = raw_p1
        if t2_prov:
            new_p2 = min(max(RATING_FLOOR, p2_before + (raw_p2 - p2_before) * t2_k), RATING_CAP)
        else:
            new_p2 = raw_p2

        teams[t1] = new_p1
        teams[t2] = new_p2

        if t1_prov and (not forfeiting_team or forfeiting_team == t1):
            increment_provisional(t1)
        if t2_prov and (not forfeiting_team or forfeiting_team == t2):
            increment_provisional(t2)

        # --- Rank shifts ---
        # dep_base only contains non-provisional teams (provisional teams were
        # skipped when it was built above). To keep old_order/new_order
        # comparable, new_order must apply that same "non-provisional only"
        # rule to t1/t2 — only insert them if they're established (or just
        # graduated via increment_provisional above), not simply because they
        # played this match. Previously t1/t2 were force-inserted into
        # dep_new unconditionally, so a still-provisional team got ranked
        # against the full established pool while every other still-
        # provisional team stayed excluded — producing meaningless rank
        # numbers/shifts (e.g. a team's very first-ever match showing
        # "Rank #60" against what was really only ~60 established teams).
        dep_new = dict(dep_base)
        if t1 in dep_new or not is_provisional(t1):
            dep_new[t1] = new_p1
        if t2 in dep_new or not is_provisional(t2):
            dep_new[t2] = new_p2
        new_order = sorted(dep_new.keys(), key=lambda x: dep_new[x], reverse=True)
        t1_rank_shift = (old_order.index(t1) + 1) - (new_order.index(t1) + 1) if (t1 in old_order and t1 in new_order) else 0
        t2_rank_shift = (old_order.index(t2) + 1) - (new_order.index(t2) + 1) if (t2 in old_order and t2 in new_order) else 0

        t1_rank_str = f"Rank #{new_order.index(t1)+1}" if t1 in new_order else "Rank: provisional (unranked)"
        t2_rank_str = f"Rank #{new_order.index(t2)+1}" if t2 in new_order else "Rank: provisional (unranked)"
        _batch_log(f"  {t1}: {int(p1_before)} -> {int(new_p1)} ({'+' if new_p1 >= p1_before else ''}{int(new_p1 - p1_before)}) | {t1_rank_str}")
        _batch_log(f"  {t2}: {int(p2_before)} -> {int(new_p2)} ({'+' if new_p2 >= p2_before else ''}{int(new_p2 - p2_before)}) | {t2_rank_str}")

        # --- Build entry ---
        entry = {
            "date": entry_date,
            "tier": tier_raw,
            "env": env_raw,
            "grand_final": is_gf,
            "match_stage": match_stage,
            "source": "HLTV Batch Import",
            "url": url_raw,
            "t1": {"name": t1, "score": s1, "pts_before": p1_before, "pts_after": new_p1, "rank_shift": t1_rank_shift},
            "t2": {"name": t2, "score": s2, "pts_before": p2_before, "pts_after": new_p2, "rank_shift": t2_rank_shift},
        }
        if event_name:
            entry["event"] = event_name
        if forfeiting_team:
            entry["forfeit"] = forfeiting_team

        is_valid, error_msg = validate_match_entry(entry)
        if not is_valid:
            _batch_log(f"  FAIL: invalid entry — {error_msg}")
            failed.append(url_raw)
            continue

        history.append(entry)
        update_peak(t1, teams.get(t1, 1000), entry_date)
        update_peak(t2, teams.get(t2, 1000), entry_date)
        mark_unsaved()
        total_imports += 1
        import_counter += 1
        if import_counter % 5 == 0:
            save_all(silent=True)
            _batch_log(f"  [Auto-saved at {import_counter} imports]")

    return import_counter, failed


def run_batch_import() -> None:
    """
    Non-interactive batch importer. Two entry points feed the same import
    core (_import_url_list):

      1. From a .txt file of HLTV match URLs (one per line) — original flow.
      2. Auto-backfill by date range — scrapes HLTV's /results listing for
         every match URL between two dates (scrape_hltv_results) and feeds
         them straight into the importer in one command, no intermediate
         file needed.

    Both paths use the same logic as import_from_hltv() — same tier
    detection, same provisional system, same forfeit handling — but without
    any input() prompts during the import loop itself.

    Auto-decisions (mirroring the interactive defaults):
      - Date missing      → use current time
      - Tier scrape fails → default to 'A'
      - Env missing       → default to 'LAN'
      - Team not found    → auto_register_team (same as interactive)

    All output is written to batch_import.log as well as stdout.
    A single BrowserSession is shared across all scrapes (and, in auto-backfill
    mode, across the results-listing scrape too).
    Progress is saved every 5 imports (same cadence as interactive mode), and
    matches already in history are skipped — so an interrupted backfill can
    just be re-run with the same date range to pick up where it left off.
    """
    print("\n--- Batch Import from HLTV ---")
    print("1. Import from a .txt file of URLs")
    print("2. Auto-backfill by date range (scrape HLTV directly)")
    print("0. Cancel")
    raw_mode = check_cmd(input("Select: ")).strip()
    mode = get_cmd(raw_mode)
    if mode in ['0', 'back']:
        return

    urls_to_do: List[str] = []

    if mode == '1':
        print("\nPaste the path to a .txt file containing one HLTV match URL per line.")
        url_file = check_cmd(input("File path (or 0 to cancel): ")).strip()
        if get_cmd(url_file) in ['0', 'back']:
            return

        if not os.path.isfile(url_file):
            print(f"  [!] File not found: {url_file}")
            return

        with open(url_file, "r", encoding="utf-8") as f:
            raw_lines = [l.strip() for l in f if l.strip()]

        urls = [l for l in raw_lines if 'hltv.org/matches/' in l]
        skipped_bad = len(raw_lines) - len(urls)
        if not urls:
            print("  [!] No valid HLTV match URLs found in file.")
            return

        imported_urls = get_imported_urls(history)
        urls_to_do = [u for u in urls if u not in imported_urls]
        already_done = len(urls) - len(urls_to_do)

        print(f"\n  URLs in file    : {len(urls)}")
        if skipped_bad:
            print(f"  Skipped (bad)   : {skipped_bad}")
        if already_done:
            print(f"  Already imported: {already_done}")
        print(f"  To import       : {len(urls_to_do)}")

    elif mode == '2':
        print("\n--- Auto-Backfill by Date Range ---")
        print("Pulls every match URL HLTV lists in this window — all tiers, no filtering.")
        default_start = "2026-01-01"
        default_end = datetime.now().strftime("%Y-%m-%d")

        raw_start = check_cmd(input(f"Start date YYYY-MM-DD [{default_start}] (or 0 to cancel): ")).strip()
        if get_cmd(raw_start) in ['0', 'back']:
            return
        start_date = raw_start or default_start

        raw_end = check_cmd(input(f"End date YYYY-MM-DD [{default_end}] (or 0 to cancel): ")).strip()
        if get_cmd(raw_end) in ['0', 'back']:
            return
        end_date = raw_end or default_end

        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            print("  [!] Dates must be in YYYY-MM-DD format.")
            return

        print(f"\n  Scraping HLTV results from {start_date} to {end_date} ...")
        print("  This pages through the results listing 100 at a time — progress logs below.\n")

        with BrowserSession() as scrape_session:
            scraped_urls = scrape_hltv_results(start_date, end_date, context=scrape_session.context)

        if not scraped_urls:
            print("  [!] No match URLs found for that date range.")
            return

        imported_urls = get_imported_urls(history)
        urls_to_do = [u for u in scraped_urls if u not in imported_urls]
        already_done = len(scraped_urls) - len(urls_to_do)

        print(f"\n  Matches found   : {len(scraped_urls)}")
        if already_done:
            print(f"  Already imported: {already_done}")
        print(f"  To import       : {len(urls_to_do)}")

        if urls_to_do:
            est_low_hrs = len(urls_to_do) * 5 / 3600
            est_high_hrs = len(urls_to_do) * 18 / 3600
            print(f"  Estimated runtime: {est_low_hrs:.1f}-{est_high_hrs:.1f} hours at 5-18s/match.")
            print("  Safe to interrupt — re-running this same date range later skips what's already imported.")

    else:
        print("  Invalid choice.")
        return

    if not urls_to_do:
        print("  Nothing to do.")
        return

    confirm = check_cmd(input(f"\n  Start batch import of {len(urls_to_do)} matches? (y/n): ")).strip().lower()
    if confirm != 'y':
        print("  Cancelled.")
        return

    _batch_log(f"=== Batch import started: {len(urls_to_do)} URLs ===")

    with BrowserSession() as session:
        import_counter, failed = _import_url_list(urls_to_do, session.context)

    _vrs_session_cache.clear()

    # --- Summary ---
    _batch_log(f"=== Batch import complete: {import_counter} imported, {len(failed)} failed ===")
    if failed:
        _batch_log("Failed URLs:")
        for u in failed:
            _batch_log(f"  {u}")

    if import_counter > 0:
        save_all()

    print(f"\n  Done. Log written to {BATCH_LOG_FILE}")
    input("Press Enter to continue...")


def manage_event_tiers() -> None:
    '''View and edit event tier mappings.'''
    while True:
        print('\n--- Manage Event Tiers ---')
        if event_tiers:
            print('Current event tier mappings:')
            for event, tier in sorted(event_tiers.items()):
                print(f'  {event}: {tier}')
        else:
            print('No event tier mappings defined.')
        print('\n1. Add/Edit Event Tier')
        print('2. Delete Event Tier')
        print('3. Resimulate All Matches (apply changes)')
        print('0. Back')
        
        raw_choice = check_cmd(input('Select: ')).strip()
        choice = get_cmd(raw_choice)
        
        if choice in ['back', '0']:
            break

        if choice == '1':
            event = check_cmd(input('Event name (or 0 to go back): ')).strip()
            if get_cmd(event) in ['back', '0']:
                continue
            current = event_tiers.get(event)
            if current:
                print(f"Current tier for '{event}': {current}")
            new_tier = check_cmd(input('New tier (S+/S/A/B/C/D): ')).strip().upper()
            if get_cmd(new_tier) in ['back', '0']:
                continue
            valid_tiers = ['S+', 'S', 'A', 'B', 'C', 'D', 'E']
            if new_tier not in valid_tiers:
                print(f"Invalid tier. Must be one of: {', '.join(valid_tiers)}")
                continue
            event_tiers[event] = new_tier
            mark_unsaved()
            save_all()
            print(f"Set tier for '{event}' to {new_tier}.")
            resim = get_cmd(check_cmd(input('Resimulate all matches now to apply this change? (y/n): ')))
            if resim == 'y':
                resimulate()

        elif choice == '2':
            event = check_cmd(input('Event name to delete (or 0 to go back): ')).strip()
            if get_cmd(event) in ['back', '0']:
                continue
            if event in event_tiers:
                del event_tiers[event]
                mark_unsaved()
                save_all()
                print(f"Deleted tier mapping for '{event}'.")
            else:
                print('Event not found.')

        elif choice == '3':
            resimulate()

def analytics_tools_menu() -> None:
    """Submenu for analytical tools and utilities."""
    options = [
        ('1', 'Team Analytics', team_analytics_menu),
        ('2', 'Event Summary', event_summary_menu),
        ('3', 'CSRS vs VRS Comparison', compare_csrs_to_vrs),
        ('4', 'CSRS vs VRS Rankings', compare_csrs_vrs_rankings),
        ('5', 'Manage Event Tiers', manage_event_tiers),
        ('6', 'Duplicate Match Detection', duplicate_match_detection),
        ('7', 'Clear History', clear_history_menu),
        ('0', 'Back', None),
    ]
    
    while True:
        print_menu(
            "ANALYTICS & TOOLS",
            [
                ("1", "Team Analytics"),
                ("2", "Event Summary"),
                (None, None),
                ("3", "CSRS vs VRS Comparison"),
                ("4", "CSRS vs VRS Rankings"),
                (None, None),
                ("5", "Manage Event Tiers"),
                ("6", "Duplicate Match Detection"),
                ("7", "Clear History"),
                (None, None),
                ("h", "Help  (explain features)"),
                ("0", "Back"),
            ],
        )
        
        raw_choice = check_cmd(input("Select: ")).strip()
        choice = get_cmd(raw_choice)
        
        if choice in ['0', 'back']:
            break
        
        if choice == 'h':
            print("\n--- Feature Help ---")
            print("  1. Team Analytics: Graphs, form tables, team comparisons")
            print("  2. Event Summary: Breaks down team performance at tournaments")
            print("  3. CSRS vs VRS: Compares our ratings against HLTV's official rankings")
            print("  4. Rankings Compare: Shows ranking position differences (CSRS # vs VRS #)")
            print("  5. Event Tiers: Set S+/S/A/B/C/D/E tiers for tournaments")
            print("  6. Duplicates: Find and remove duplicate match entries")
            print("  7. Clear History: Reset all match data (with backup)")
            print("\n  Tip: Use '0' or 'back' at any prompt to return.")
            input("\nPress Enter to continue...")
            continue
        
        found = False
        for num, _, func in options:
            if choice == num:
                func()
                found = True
                break
        if not found:
            print_warning("Invalid choice. Try again.")

# =============================================================================
# === MAIN ENTRY POINT ===
# =============================================================================

# =============================================================================
# === GIT PUSH ===
# =============================================================================

def _git_push() -> bool:
    """
    Stage data.save, commit, and push to origin.
    Uses GITHUB_TOKEN env var for auth if set:
      GITHUB_TOKEN=ghp_xxx  (repo must be cloned with HTTPS)
    Falls back to whatever git credentials are already configured.
    Returns True on success.
    """
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    token = os.environ.get("GITHUB_TOKEN", "")

    # If token provided, rewrite the remote URL to embed it
    if token:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_dir, capture_output=True, text=True
            )
            remote_url = result.stdout.strip()
            # Only rewrite if it's an HTTPS remote
            if remote_url.startswith("https://") and "@" not in remote_url:
                # https://github.com/user/repo -> https://token@github.com/user/repo
                authed_url = remote_url.replace("https://", f"https://{token}@")
                subprocess.run(
                    ["git", "remote", "set-url", "origin", authed_url],
                    cwd=repo_dir, capture_output=True
                )
        except Exception as e:
            _batch_log(f"  [WARN] Could not set git token URL: {e}")

    try:
        # Stage data.save only — never commit source code automatically
        subprocess.run(
            ["git", "add", SAVE_FILE],
            cwd=repo_dir, check=True, capture_output=True
        )

        # Check if there's actually anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain", SAVE_FILE],
            cwd=repo_dir, capture_output=True, text=True
        )
        if not status.stdout.strip():
            _batch_log("  [GIT] Nothing to commit — data.save unchanged.")
            return True

        # Commit with timestamp
        commit_msg = f"auto: data update {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir, check=True, capture_output=True
        )

        # Push
        push = subprocess.run(
            ["git", "push"],
            cwd=repo_dir, capture_output=True, text=True
        )
        if push.returncode == 0:
            _batch_log("  [GIT] Pushed data.save to origin successfully.")
            return True
        else:
            _batch_log(f"  [GIT ERROR] Push failed: {push.stderr.strip()}")
            return False

    except subprocess.CalledProcessError as e:
        _batch_log(f"  [GIT ERROR] {e.cmd}: {e.stderr.decode().strip() if e.stderr else str(e)}")
        return False
    except Exception as e:
        _batch_log(f"  [GIT ERROR] Unexpected error: {e}")
        return False


# =============================================================================
# === AUTO IMPORT (headless daemon mode) ===
# =============================================================================

def run_auto_import(lookback_hours: int = 48) -> int:
    """
    Headless single-pass auto-import.
    Scrapes currently active HLTV events for new matches,
    imports anything not already in history, saves, then pushes to GitHub.
    Returns number of matches imported (0 if nothing new or error).
    """
    now = datetime.now()
    start_dt = now - timedelta(hours=lookback_hours)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    _batch_log(f"=== Auto-import started "
               f"(lookback {lookback_hours}h: {start_date} -> {end_date}) ===")

    cookies = _load_hltv_cookies()

    # --- Scrape phase: get active events then scrape each ---
    try:
        _batch_log("  Fetching active events from HLTV...")
        active_events = scrape_active_events(cookies=cookies)
        if not active_events:
            _batch_log("  No active events found — nothing to import.")
            return 0
        _batch_log(f"  Found {len(active_events)} active events")

        scraped_urls = []
        seen: set[str] = set()
        for evt in active_events:
            _batch_log(f"  Scraping event {evt['id']}: {evt['name'][:50]}")
            urls = scrape_hltv_results_by_event(evt["id"], cookies=cookies)
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    scraped_urls.append(u)
            _batch_log(f"    -> {len(urls)} match URLs")

    except Exception as e:
        _batch_log(f"  [ERROR] Scrape phase failed: {e}")
        return 0

    if not scraped_urls:
        _batch_log("  No match URLs found — nothing to import.")
        return 0

    # --- Filter already-imported ---
    imported_urls = get_imported_urls(history)
    urls_to_do = [u for u in scraped_urls if u not in imported_urls]
    already_done = len(scraped_urls) - len(urls_to_do)
    _batch_log(f"  Found {len(scraped_urls)} matches scraped, "
               f"{already_done} already imported, "
               f"{len(urls_to_do)} new to import.")

    if not urls_to_do:
        _batch_log("  Nothing new to import.")
        return 0

    # --- Import phase ---
    try:
        with BrowserSession() as session:
            import_counter, failed = _import_url_list(urls_to_do, session.context)
    except Exception as e:
        _batch_log(f"  [ERROR] Import phase failed: {e}")
        return 0

    # --- Save ---
    _vrs_session_cache.clear()
    save_all(silent=True)

    if failed:
        _batch_log(f"  [WARN] {len(failed)} match(es) failed to import:")
        for u in failed:
            _batch_log(f"    FAILED: {u}")

    _batch_log(f"=== Auto-import complete: "
               f"{import_counter} imported, {len(failed)} failed ===")

    # --- Git push (only if something was imported) ---
    if import_counter > 0:
        _git_push()

    return import_counter


# =============================================================================
# --delete command — wipes data/save/logs under CSRS_DATA_DIR, with
# hltv_cookies.json hard-excluded under every circumstance. This is a
# standalone, self-contained routine: it does NOT call load_all(), does NOT
# require dependency checks, and does NOT touch any in-memory CSRS state.
# =============================================================================

# Absolute, normalized path to the one file that must never be deleted by
# --delete, regardless of which target ("data", "save", "logs", "all") is
# requested. Computed once here from the same CSRS_DATA_DIR/data/hltv_cookies.json
# convention used everywhere else in this file (see HLTV_COOKIE_FILE above).
_PROTECTED_COOKIE_FILE = os.path.normcase(os.path.normpath(os.path.abspath(
    os.path.join(os.environ.get("CSRS_DATA_DIR", "."), "data", "hltv_cookies.json")
)))


def _is_protected_path(path: str) -> bool:
    """
    Returns True if `path` IS the cookie file, or is a directory that
    CONTAINS the cookie file (in which case the directory must be emptied
    around it rather than removed wholesale).
    """
    norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    if norm == _PROTECTED_COOKIE_FILE:
        return True
    # Directory case: does the protected file live inside this directory?
    try:
        common = os.path.commonpath([norm, _PROTECTED_COOKIE_FILE])
        return common == norm
    except ValueError:
        # Different drives on Windows, or unrelated paths — not protected.
        return False


def _delete_path_preserving_cookies(path: str) -> tuple[int, int, int]:
    """
    Deletes the FILES inside `path` (recursively), but never removes any
    directory — only file contents are wiped, the entire folder skeleton
    (e.g. save/, save/main/, save/backup/) is always left standing. This
    matters because CSRS.py assumes save/main and save/backup already
    exist when writing SAVE_FILE / backups, with no os.makedirs guard
    before those writes — losing the directories (not just their
    contents) would silently break the next save/backup write.

    hltv_cookies.json is never removed under any circumstance — if it
    lives inside `path`, every other file around it is still wiped
    normally; the cookie file itself is always skipped.

    Every file is deleted individually (no shutil.rmtree) so a single
    locked/in-use file (e.g. a log file held open by a running daemon)
    is skipped with a warning instead of aborting the entire operation
    and leaving everything else undeleted.

    Returns (files_deleted, dirs_deleted, files_locked). dirs_deleted is
    always 0 now — kept in the return signature for call-site compatibility.
    """
    files_deleted = 0
    dirs_deleted = 0  # directories are never removed; kept for compatibility
    files_locked = 0

    if not os.path.exists(path):
        return (0, 0, 0)

    norm_path = os.path.normcase(os.path.normpath(os.path.abspath(path)))

    # Exact-match safety net: never, ever delete the cookie file itself,
    # no matter how it was reached.
    if norm_path == _PROTECTED_COOKIE_FILE:
        print(f"  [PROTECTED] Skipping {path} — hltv_cookies.json is never deleted.")
        return (0, 0, 0)

    if os.path.isfile(path):
        try:
            os.remove(path)
            return (1, 0, 0)
        except OSError as e:
            print(f"  [WARN] Could not delete {path} (in use?): {e}")
            return (0, 0, 1)

    # Directory case — walk every file in the tree and delete it, but
    # never call os.rmdir on anything. The directory structure itself
    # (save/, save/main/, save/backup/, logs/normal/, etc.) is always
    # preserved, even when fully emptied.
    for root, dirs, files in os.walk(path, topdown=False):
        for fname in files:
            fpath = os.path.join(root, fname)
            if os.path.normcase(os.path.normpath(os.path.abspath(fpath))) == _PROTECTED_COOKIE_FILE:
                print(f"  [PROTECTED] Keeping {fpath}")
                continue
            try:
                os.remove(fpath)
                files_deleted += 1
            except OSError as e:
                print(f"  [WARN] Could not delete {fpath} (in use?): {e}")
                files_locked += 1
        # Intentionally no os.rmdir() here — directories are never removed.

    return (files_deleted, dirs_deleted, files_locked)


def run_delete_command(target: str, skip_confirm: bool = False) -> None:
    """
    Implements `python CSRS.py --delete data|save|logs|all`.

    hltv_cookies.json is hard-excluded from deletion under every target,
    including "all" — this is non-negotiable because the scraper cannot
    log in to HLTV without it.
    """
    # logging.basicConfig() at module load time opens a FileHandler on
    # logs/normal/csrs.log that stays open for the life of this process —
    # including right now. If we don't close it first, --delete (and
    # --delete logs/all in particular) will ALWAYS fail to remove csrs.log
    # because this same process is holding it open, regardless of whether
    # any other CSRS process is running. Close and detach every handler
    # before touching the filesystem.
    for _h in list(logger.handlers):
        try:
            _h.close()
        except Exception:
            pass
        logger.removeHandler(_h)
    for _h in list(logging.getLogger().handlers):
        try:
            _h.close()
        except Exception:
            pass
        logging.getLogger().removeHandler(_h)

    data_dir = os.environ.get("CSRS_DATA_DIR", ".")

    target = target.strip().lower()
    valid_targets = {"data", "save", "logs", "all"}
    if target not in valid_targets:
        print(f"[ERROR] Unknown --delete target '{target}'. Must be one of: {', '.join(sorted(valid_targets))}")
        sys.exit(1)

    target_dirs_by_name = {
        "data": [os.path.join(data_dir, "data")],
        "save": [os.path.join(data_dir, "save")],
        "logs": [os.path.join(data_dir, "logs")],
    }
    target_dirs_by_name["all"] = (
        target_dirs_by_name["data"] + target_dirs_by_name["save"] + target_dirs_by_name["logs"]
    )

    dirs_to_wipe = target_dirs_by_name[target]

    print(f"\n{'='*60}")
    print(f"CSRS --delete {target}")
    print(f"{'='*60}")
    print(f"CSRS_DATA_DIR : {os.path.abspath(data_dir)}")
    print(f"Will wipe     : {', '.join(os.path.abspath(d) for d in dirs_to_wipe)}")
    print(f"PROTECTED     : {_PROTECTED_COOKIE_FILE}  (never deleted, any target)")
    print(f"{'='*60}\n")

    existing_dirs = [d for d in dirs_to_wipe if os.path.exists(d)]
    if not existing_dirs:
        print("Nothing to delete — none of the target directories exist.")
        return

    if not skip_confirm:
        confirm = input(
            f"Type 'yes' to permanently delete the FILES inside {target} "
            f"(folder structure and hltv_cookies.json will be preserved): "
        ).strip().lower()
        if confirm != "yes":
            print("Cancelled — nothing was deleted.")
            return

    total_files = 0
    total_locked = 0
    for d in existing_dirs:
        print(f"Deleting files in {d} ...")
        f, _dd, locked = _delete_path_preserving_cookies(d)
        total_files += f
        total_locked += locked
        msg = f"  Removed {f} file(s). Folder structure preserved."
        if locked:
            msg += f" ({locked} skipped — in use)"
        print(msg)

    print(f"\n{'='*60}")
    print(f"Delete complete: {total_files} file(s) removed. Folder structure (save/, save/main/, "
          f"save/backup/, logs/normal/, etc.) was preserved — only file contents were wiped.")
    if total_locked:
        print(f"[WARN] {total_locked} file(s) could not be deleted because they were in use "
              f"(likely a running daemon/import holding a log file open). Close any running "
              f"CSRS processes and re-run --delete to remove them.")
    if os.path.exists(_PROTECTED_COOKIE_FILE):
        print(f"hltv_cookies.json preserved at: {_PROTECTED_COOKIE_FILE}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    # -------------------------------------------------------------------------
    # CLI argument handling
    # --auto          : single headless pass (last 2 hours), then exit
    # --daemon        : loop every 30 minutes indefinitely (Ctrl+C to stop)
    # --lookback N    : override lookback window in hours (default: 2)
    # -------------------------------------------------------------------------
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--auto",    action="store_true",
                        help="Run one headless auto-import pass and exit")
    parser.add_argument("--daemon",  action="store_true",
                        help="Loop auto-import every 30 minutes until Ctrl+C")
    parser.add_argument("--lookback", type=int, default=2, metavar="HOURS",
                        help="How many hours back to scrape (default: 2)")
    parser.add_argument("--delete", type=str, default=None, metavar="data|save|logs|all",
                        help="Wipe the given data directory/directories under CSRS_DATA_DIR. "
                             "hltv_cookies.json is NEVER deleted, no matter what. Requires "
                             "interactive y/n confirmation.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt for --delete (use with caution)")
    parser.add_argument("--resimulate", action="store_true",
                        help="Resimulate all matches in history from scratch using the "
                             "current formula/config, then save and exit.")
    args, _ = parser.parse_known_args()

    if args.delete:
        run_delete_command(args.delete, skip_confirm=args.yes)
        sys.exit(0)

    # === DEPENDENCY CHECK & AUTO-INSTALL ===
    if not check_and_install_dependencies():
        sys.exit(0)

    if args.resimulate:
        os.environ["NODE_NO_WARNINGS"] = "1"
        run_resimulate_command(skip_confirm=args.yes)
        sys.exit(0)

    os.environ["NODE_NO_WARNINGS"] = "1"
    load_all()

    if args.auto or args.daemon:
        DAEMON_INTERVAL_MINUTES = 30
        FORCE_TRIGGER_FILE = os.path.join(
            os.environ.get("CSRS_DATA_DIR", "."), "force_import.trigger"
        )

        def _sleep_with_trigger_check(total_seconds: int):
            """
            Sleep in short increments, checking for a force-trigger file.
            If found, delete it and return early so the daemon re-runs
            the import immediately instead of waiting out the full interval.
            Lets the admin panel "Run Import Now" button wake a sleeping
            daemon instead of spawning a separate, disconnected process.
            """
            checked_every = 5  # seconds
            elapsed = 0
            while elapsed < total_seconds:
                if os.path.exists(FORCE_TRIGGER_FILE):
                    try:
                        os.remove(FORCE_TRIGGER_FILE)
                    except OSError:
                        pass
                    _batch_log("  [TRIGGER] Force-import signal received — running now.")
                    return
                time.sleep(checked_every)
                elapsed += checked_every

        if args.daemon:
            _batch_log(
                f"=== CSRS Daemon started — "
                f"checking every {DAEMON_INTERVAL_MINUTES} min, "
                f"lookback {args.lookback}h ==="
            )
            print(f"CSRS daemon running. Ctrl+C to stop.")
            # Clear any stale trigger file from a previous run on startup
            if os.path.exists(FORCE_TRIGGER_FILE):
                try:
                    os.remove(FORCE_TRIGGER_FILE)
                except OSError:
                    pass
            try:
                while True:
                    run_auto_import(lookback_hours=args.lookback)
                    next_run = datetime.now() + timedelta(minutes=DAEMON_INTERVAL_MINUTES)
                    _batch_log(
                        f"  Next check at "
                        f"{next_run.strftime('%H:%M:%S')} — "
                        f"sleeping {DAEMON_INTERVAL_MINUTES} min "
                        f"(or until force-triggered)."
                    )
                    _sleep_with_trigger_check(DAEMON_INTERVAL_MINUTES * 60)
            except KeyboardInterrupt:
                _batch_log("=== CSRS Daemon stopped by user ===")
                print("\nDaemon stopped.")
                sys.exit(0)
        else:
            # --auto: single pass then exit
            run_auto_import(lookback_hours=args.lookback)
            sys.exit(0)
    
    def confirm_exit() -> bool:
        """Check for unsaved changes and prompt user before exiting."""
        global unsaved_changes
        if unsaved_changes:
            print("\n[!] WARNING: You have unsaved changes.")
            resp = input("Save to data.save before exiting? (y/n): ").strip().lower()
            if resp == 'y':
                save_all()
                return True
            elif resp == 'n':
                return True
            else:
                return False
        return True

    try:
        while True:
            print_menu(
                "CSRS  ·  ELO RATING SYSTEM",
                [
                    ("1", "Import Match from HLTV"),
                    ("2", "View Rankings"),
                    ("3", "Match History"),
                    ("4", "Simulate Match"),
                    (None, None),
                    ("5", "Team Management"),
                    ("6", "Save / Load"),
                    ("7", "Analytics & Tools"),
                    ("8", "Batch Import"),
                    ("9", "Future Updates"),
                    (None, None),
                    ("0", "Exit"),
                ],
            )
            
            raw_choice = check_cmd(input("Select: "))
            choice = get_cmd(raw_choice)
            
            try:
                if choice == '1':
                    import_from_hltv(
                        teams_dict=teams,
                        history_list=history,
                        find_team_func=find_team,
                        save_func=save_all, 
                        update_peak_func=update_peak,
                        event_tiers_dict=event_tiers,
                        calculate_points_func=calculate_points,
                        old_roster_check_func=None
                    )
                elif choice == '2':
                    display_rankings()
                elif choice == '3':
                    view_match_history()
                elif choice == '4':
                    simulate_match()
                elif choice == '5':
                    team_management_menu()
                elif choice == '6':
                    save_load_menu()
                elif choice == '7':
                    analytics_tools_menu()
                elif choice == '8':
                    run_batch_import()
                elif choice == '9':
                    future_updates_menu()
                elif choice == '0':
                    if confirm_exit():
                        break
            except MenuException:
                continue
    except KeyboardInterrupt:
        print("\n\nInterrupt detected.")
        if confirm_exit():
            print("Exiting...")
        else:
            print("Resuming")