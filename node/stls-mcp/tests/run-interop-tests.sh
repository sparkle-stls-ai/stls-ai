#!/usr/bin/env bash
# run-interop-tests.sh
# ---------------------
# Runs the cross-language Node<->Python interoperability test suite for
# stls-mcp, automating the manual sequence used to validate this port
# (see CHANGELOG.md's "Validated" section for what each step proved the
# first time it was run by hand).
#
# Assumes (from the repo root):
#   - node/stls-core and node/stls-mcp: npm install + npm run build already run
#   - python/stls-core/.venv: a single shared venv with both stls_core
#     ("uv pip install -e '.[dev]'" + "liboqs-python==0.16.0.1" for the PQC
#     steps) and stls_mcp ("pip install -e '.[dev,pqc]'" from
#     python/stls-mcp, into that SAME venv) already installed — exactly
#     what .github/workflows/stls-mcp-node-interop.yml does. stls-mcp's
#     pyproject.toml resolves stls_core as a local path dependency, so
#     both packages must live in one venv for the editable install to
#     actually pick up this repo's sources (a separate python/stls-mcp/.venv
#     has caused a real, silent bug before: an unrelated editable install
#     left over from copying/moving this project pointed stls_mcp's import
#     at a completely different, unrelated checkout on disk).
#
# python/stls-mcp/.env (if present) is deliberately NOT relied upon here —
# it may hold this developer's own custom paths/auth/backend settings
# (MCP_PLAIN_PATH, AUTH_ENABLED, KEY_PROVIDER_NAME, Redis/Postgres...).
# Every Python invocation below sets its own env explicitly, so this
# script's behavior doesn't depend on whatever that file currently says.
#
# Environment variables:
#   PYTHON_VENV        path to the venv's python (default: python/stls-core/.venv/bin/python
#                      — the shared venv, see "Assumes" above)
#   SERVER_PORT         port both vault servers bind to in turn (default: 8765)
#   SKIP_PQC            if "1", skip the PQC cross-language steps (require liboqs installed
#                       on the Python side)
#   SKIP_ARQIT_SIDECAR  if "1" (or ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG are unset),
#                       skip the real arqit_py step
#   ARQIT_PY_CLIENT_CONFIG / ARQIT_PY_SERVER_CONFIG
#                       if both set (to a real, already-provisioned Arqit device pair's
#                       JSON configs), also run vault-server.ts/vault-client.ts for real
#                       with KEY_PROVIDER_NAME=arqit_py, through two separate sidecar
#                       processes (auto-detected, no flag needed to opt in). See
#                       README.md's "With real Arqit peering" section and
#                       python/stls-core/sidecar/ARQIT_PEERING_NOTES.md for the cwd
#                       caveat about relative paths inside those device JSON configs —
#                       this script runs the Python sidecar from python/stls-mcp/, where
#                       this repo's own device pair's metadata files live; a different
#                       device pair may need a different cwd.
#   ARQIT_SIDECAR_PYTHON
#                       interpreter used ONLY for the two arqit_sidecar.py processes in
#                       step [7/7] (default: python/stls-core/.venv-arqit-sidecar/bin/python,
#                       a SEPARATE venv from $PYTHON_VENV). Required because the arqit_py
#                       wheels declare "jwt==1.3.1" (python-jwt, providing `jwt.JWT`) while
#                       `mcp` (a stls_mcp dependency, installed in $PYTHON_VENV) pulls in
#                       PyJWT — both packages install to the same top-level `jwt/` import
#                       path, so whichever is installed last silently overwrites the
#                       other's files. In $PYTHON_VENV this leaves `from jwt import JWT`
#                       (used by arqit_py's AuthenticationSessionManager) raising
#                       ImportError, breaking the sidecar with no indication it's a
#                       dependency conflict rather than a real peering failure. Build this
#                       venv once with (from python/stls-core/):
#                         python3 -m venv .venv-arqit-sidecar
#                         .venv-arqit-sidecar/bin/python -m pip install -e ".[dev,arqit_py]" \
#                           --find-links arqit_wheels/arqit-skaplatform-common/ \
#                           --find-links arqit_wheels/arqit-skaplatform-policy/ \
#                           --find-links arqit_wheels/arqit-skaplatform-registration/ \
#                           --find-links arqit_wheels/arqit-skaplatform-provisioning/ \
#                           --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \
#                           --find-links arqit_wheels/arqit-skaplatform-peering/ \
#                           --find-links arqit_wheels/arqit-skaplatform-monitoring/
#                       (never installs mcp/stls_mcp/pyjwt, so no conflict).
#
# Usage:
#   ./tests/run-interop-tests.sh

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."  # node/stls-mcp/
REPO_ROOT="$(cd ../.. && pwd)"

PYTHON_VENV="${PYTHON_VENV:-$REPO_ROOT/python/stls-core/.venv/bin/python}"
ARQIT_SIDECAR_PYTHON="${ARQIT_SIDECAR_PYTHON:-$REPO_ROOT/python/stls-core/.venv-arqit-sidecar/bin/python}"
PYTHON_MCP_DIR="$REPO_ROOT/python/stls-mcp"
SERVER_PORT="${SERVER_PORT:-8765}"
SKIP_PQC="${SKIP_PQC:-0}"
SKIP_ARQIT_SIDECAR="${SKIP_ARQIT_SIDECAR:-0}"

BASE_URL="http://127.0.0.1:$SERVER_PORT"
SIDECAR_SOCKET_SERVER="/tmp/stls-mcp-arqit-sidecar-server-ci-$$.sock"
SIDECAR_SOCKET_CLIENT="/tmp/stls-mcp-arqit-sidecar-client-ci-$$.sock"

# Shared secret for the sidecar's socket auth (#17/#18) — generated once
# per script run and exported, so both sidecar processes and both
# npx tsx invocations below inherit it with no per-line changes needed.
export ARQIT_SIDECAR_TOKEN="$(openssl rand -hex 32)"

# Every Python vault_server.py/vault_client.py invocation gets this exact
# env — fixed paths/auth/backend, overriding whatever python/stls-mcp/.env
# might otherwise set (see header comment).
PY_COMMON_ENV=(
  MCP_PLAIN_PATH=/mcp
  MCP_STLS_PATH=/mcp-stls
  MCP_SERVER_PORT="$SERVER_PORT"
  AUTH_ENABLED=false
  SESSION_CACHE_BACKEND=memory
  KEY_STORE_BACKEND=memory
)

CURRENT_SERVER_PID=""
# Literal, reliably-contiguous substrings to match against the REAL process's
# full cmdline — not "tsx tests/vault-server.ts", which is NOT contiguous in
# the actual node child process spawned by `npx tsx ...` (there's a
# `tsx/dist/loader.mjs` in between "tsx" and "tests/vault-server.ts"), so a
# pkill -f on that literal silently fails to match and leaves a zombie bound
# to the port.
NODE_SERVER_PATTERN="tests/vault-server.ts"
PYTHON_SERVER_PATTERN="tests/vault_server.py"

cleanup() {
  pkill -f "$NODE_SERVER_PATTERN" >/dev/null 2>&1 || true
  pkill -f "$PYTHON_SERVER_PATTERN" >/dev/null 2>&1 || true
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET_SERVER" >/dev/null 2>&1 || true
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET_CLIENT" >/dev/null 2>&1 || true
  rm -f "$SIDECAR_SOCKET_SERVER" "$SIDECAR_SOCKET_CLIENT"
}
trap cleanup EXIT

# Polls for the socket file instead of a fixed sleep — more reliable
# under CI load, and avoids confusing "connection refused" (sidecar not
# listening yet) with an auth rejection in failures.
wait_for_socket() {
  local path="$1"
  for _ in $(seq 1 50); do
    [ -S "$path" ] && return 0
    sleep 0.1
  done
  echo "ERROR: $path never appeared" >&2
  return 1
}

# Waits for something to be listening on $BASE_URL — a plain sleep isn't
# reliable enough across two different runtimes (Node's http.Server vs.
# uvicorn) with different startup costs.
wait_for_server() {
  local tries=0
  while ! curl -s -o /dev/null "$BASE_URL/mcp" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 50 ]; then
      echo "Server on $BASE_URL did not come up in time" >&2
      return 1
    fi
    sleep 0.2
  done
}

# Waits until nothing is actually bound to $SERVER_PORT, via a real bind
# attempt (not a connect-based check — a listening socket mid-TIME_WAIT is a
# different condition than "nobody is accepting connections there"; see
# python/stls-core/tests/test_arqit_py_real_peering.py's _wait_port_free()
# for the same lesson learned on the Python side).
wait_port_free() {
  local tries=0
  while ! python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('127.0.0.1', $SERVER_PORT))
except OSError:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 50 ]; then
      echo "Port $SERVER_PORT did not free up in time" >&2
      return 1
    fi
    sleep 0.2
  done
}

start_node_server() {
  local key_provider="$1"
  KEY_PROVIDER_NAME="$key_provider" MCP_SERVER_PORT="$SERVER_PORT" MCP_SERVER_HOST=127.0.0.1 \
    npx tsx tests/vault-server.ts &
  CURRENT_SERVER_PID=$!
  wait_for_server
}

start_python_server() {
  local key_provider="$1"
  (cd "$PYTHON_MCP_DIR" && env "${PY_COMMON_ENV[@]}" KEY_PROVIDER_NAME="$key_provider" "$PYTHON_VENV" tests/vault_server.py) &
  CURRENT_SERVER_PID=$!
  wait_for_server
}

# Kills the whole process tree behind the server we just started, not just
# the immediate backgrounded job: `npx tsx ...` and the Python venv launcher
# each spawn a child a level or two below the PID captured in $!, and a plain
# `kill "$CURRENT_SERVER_PID"` leaves that real child (still bound to the
# port) running. pkill -f against the literal script path matches every
# process in the tree (npx wrapper, tsx loader, final node process; venv
# shim, final python process) because that path appears, unmodified, as a
# trailing argument in every one of them.
stop_server() {
  pkill -f "$NODE_SERVER_PATTERN" >/dev/null 2>&1 || true
  pkill -f "$PYTHON_SERVER_PATTERN" >/dev/null 2>&1 || true
  wait "$CURRENT_SERVER_PID" 2>/dev/null || true
  CURRENT_SERVER_PID=""
  wait_port_free
}

echo "== [1/7] Plain-endpoint check (official, unmodified MCP client against Node's /mcp) =="
start_node_server base
VAULT_SERVER_URL="$BASE_URL" npx tsx tests/interop/plain-endpoint.smoke.ts
stop_server

echo "== [2/7] Node server + Node client, same language (/mcp-stls, base provider) =="
start_node_server base
VAULT_CLIENT_TARGET_URL="$BASE_URL" npx tsx tests/vault-client.ts
stop_server

echo "== [3/7] Cross-language: Node server + Python client (base provider) =="
start_node_server base
(cd "$PYTHON_MCP_DIR" && env "${PY_COMMON_ENV[@]}" KEY_PROVIDER_NAME=base VAULT_CLIENT_TARGET_URL="$BASE_URL" "$PYTHON_VENV" tests/vault_client.py)
stop_server

echo "== [4/7] Cross-language: Python server + Node client (base provider) =="
start_python_server base
VAULT_CLIENT_TARGET_URL="$BASE_URL" npx tsx tests/vault-client.ts
stop_server

if [ "$SKIP_PQC" != "1" ]; then
  echo "== [5/7] Cross-language: Node server + Python client (pqc provider) =="
  start_node_server pqc
  (cd "$PYTHON_MCP_DIR" && env "${PY_COMMON_ENV[@]}" KEY_PROVIDER_NAME=pqc VAULT_CLIENT_TARGET_URL="$BASE_URL" "$PYTHON_VENV" tests/vault_client.py)
  stop_server

  echo "== [6/7] Cross-language: Python server + Node client (pqc provider) =="
  start_python_server pqc
  KEY_PROVIDER_NAME=pqc VAULT_CLIENT_TARGET_URL="$BASE_URL" npx tsx tests/vault-client.ts
  stop_server
else
  echo "== [5/7] Node server + Python client (pqc) SKIPPED (SKIP_PQC=1) =="
  echo "== [6/7] Python server + Node client (pqc) SKIPPED (SKIP_PQC=1) =="
fi

if [ "$SKIP_ARQIT_SIDECAR" != "1" ] && [ -n "${ARQIT_PY_CLIENT_CONFIG:-}" ] && [ -n "${ARQIT_PY_SERVER_CONFIG:-}" ]; then
  echo "== [7/7] Real Arqit peering: Node server + Node client (arqit_py, two separate sidecar processes) =="
  rm -f "$SIDECAR_SOCKET_SERVER" "$SIDECAR_SOCKET_CLIENT"
  (cd "$PYTHON_MCP_DIR" && "$ARQIT_SIDECAR_PYTHON" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET_SERVER") &
  SERVER_SIDECAR_PID=$!
  (cd "$PYTHON_MCP_DIR" && "$ARQIT_SIDECAR_PYTHON" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET_CLIENT") &
  CLIENT_SIDECAR_PID=$!
  wait_for_socket "$SIDECAR_SOCKET_SERVER"
  wait_for_socket "$SIDECAR_SOCKET_CLIENT"

  KEY_PROVIDER_NAME=arqit_py ARQIT_SIDECAR_SOCKET_SERVER="$SIDECAR_SOCKET_SERVER" \
    MCP_SERVER_PORT="$SERVER_PORT" MCP_SERVER_HOST=127.0.0.1 \
    npx tsx tests/vault-server.ts &
  CURRENT_SERVER_PID=$!
  wait_for_server

  KEY_PROVIDER_NAME=arqit_py ARQIT_SIDECAR_SOCKET_CLIENT="$SIDECAR_SOCKET_CLIENT" \
    VAULT_CLIENT_TARGET_URL="$BASE_URL" npx tsx tests/vault-client.ts
  stop_server

  kill "$SERVER_SIDECAR_PID" "$CLIENT_SIDECAR_PID" 2>/dev/null || true
  wait "$SERVER_SIDECAR_PID" "$CLIENT_SIDECAR_PID" 2>/dev/null || true
else
  echo "== [7/7] Real Arqit peering SKIPPED (ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG not set, or SKIP_ARQIT_SIDECAR=1) =="
fi

echo "== ALL STLS-MCP INTEROPERABILITY TESTS PASSED =="
