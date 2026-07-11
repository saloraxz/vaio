#!/bin/sh
# CSRS Daemon entrypoint
# Runs backfill.py first (skips already-completed windows via checkpoint),
# then falls into the normal daemon loop indefinitely.

set -e

echo "=== CSRS Entrypoint: starting ==="
echo "    Working dir: $(pwd)"
echo "    Data dir:    ${CSRS_DATA_DIR:-/data}"

# CSRS.py and backfill.py are always at /app (set by Dockerfile.daemon WORKDIR)
cd /app

BACKFILL="/app/backfill.py"
CHECKPOINT="${CSRS_DATA_DIR:-/data}/data/backfill_progress.json"

if [ -f "$BACKFILL" ]; then
    if [ -f "$CHECKPOINT" ]; then
        echo ""
        echo "=== Resuming backfill from checkpoint ==="
        python "$BACKFILL" --continue
        echo ""
        echo "=== Backfill resume complete — starting daemon ==="
    elif [ "${CSRS_RUN_BACKFILL:-0}" = "1" ]; then
        echo ""
        echo "=== Running backfill (Jan 1 2026 → today) ==="
        echo "    Checkpoint file: $CHECKPOINT"
        echo ""
        python "$BACKFILL" --import --start 2026-01-01
        echo ""
        echo "=== Backfill complete — starting daemon ==="
    else
        echo ""
        echo "=== Backfill skipped (set CSRS_RUN_BACKFILL=1 to run) ==="
    fi
else
    echo "[WARN] backfill.py not found at $BACKFILL — skipping straight to daemon"
fi

echo ""
exec python CSRS.py --daemon --lookback-days 2
