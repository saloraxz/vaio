#!/bin/sh
# CSRS Daemon entrypoint
# Runs backfill.py first (skips already-completed windows via checkpoint),
# then falls into the normal daemon loop indefinitely.

set -e

echo "=== CSRS Entrypoint: starting ==="
echo "    Working dir: $(pwd)"
echo "    Data dir:    ${CSRS_DATA_DIR:-/data}"

# backfill.py sits next to CSRS.py in /app
BACKFILL="/app/backfill.py"

if [ -f "$BACKFILL" ]; then
    echo ""
    echo "=== Running backfill (Jan 1 2026 → today) ==="
    echo "    Checkpoint file: ${CSRS_DATA_DIR:-/data}/backfill_progress.json"
    echo "    Completed windows will be skipped automatically."
    echo ""
    python "$BACKFILL" --start 2026-01-01
    echo ""
    echo "=== Backfill complete — starting daemon ==="
else
    echo "[WARN] backfill.py not found at $BACKFILL — skipping straight to daemon"
fi

echo ""
exec python CSRS.py --daemon --lookback 48
