#!/usr/bin/env bash
# Stops everything started by `scripts/dev-all.sh` when it was run in
# the background (so there's no terminal to Ctrl+C). Reads the PID
# dev-all.sh recorded in .dev-all.pid and signals that whole process
# group — the same `kill 0` mechanism dev-all.sh's own trap uses on
# Ctrl+C, just triggered from the outside instead.
#
# Usage:
#   bash scripts/stop-all.sh     # or: npm run stop:all

set -eu
cd "$(dirname "$0")/.."

if [ ! -f .dev-all.pid ]; then
  echo "[stop-all] .dev-all.pid not found — dev-all.sh doesn't look like it's running."
  exit 0
fi

pid="$(cat .dev-all.pid)"

if ! kill -0 "$pid" 2>/dev/null; then
  echo "[stop-all] PID $pid from .dev-all.pid is no longer running — cleaning up stale pidfile."
  rm -f .dev-all.pid
  exit 0
fi

echo "[stop-all] stopping dev-all.sh (PID $pid) and its process group..."
kill -- "-$pid" 2>/dev/null || true
rm -f .dev-all.pid
echo "[stop-all] done."
