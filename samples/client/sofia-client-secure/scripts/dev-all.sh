#!/usr/bin/env bash
# Starts all three dev-mode processes this project needs (README.md
# steps 6-8) in one shell: the Python secure transport service, the
# Express backend, and the React dev server.
#
# Running in the foreground: Ctrl+C stops all three together — `kill 0`
# signals the whole process group this script and its background jobs
# belong to.
# Running in the background (`npm run dev:all &`, or started from a
# tool with no terminal to Ctrl+C in): use `scripts/stop-all.sh` —
# this script records its own PID in .dev-all.pid for it to find.
#
# Prerequisites unchanged from the README: Python deps installed,
# `npm install` run, `.env` configured. Does not start the
# key-exchange sidecar — not needed for KEY_PROVIDER_NAME=pqc (the
# default in .env.example), only for base/arqit_py.
#
# Usage:
#   bash scripts/dev-all.sh      # or: npm run dev:all
#   bash scripts/stop-all.sh     # or: npm run stop:all

set -eu
cd "$(dirname "$0")/.."

echo $$ > .dev-all.pid
trap 'rm -f .dev-all.pid; kill 0' EXIT INT TERM

echo "[dev-all] secure transport service -> http://localhost:8090 (health: /health)"
python python/secure_transport.py &

echo "[dev-all] Express backend          -> http://localhost:3000"
npm run dev:server &

echo "[dev-all] React dev server         -> http://localhost:5173"
npm run dev &

wait
