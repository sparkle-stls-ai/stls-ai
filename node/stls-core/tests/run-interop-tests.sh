#!/usr/bin/env bash
# run-interop-tests.sh
# ---------------------
# Runs the full cross-language Node<->Python interoperability test suite
# for stls-core, automating the manual sequence documented in README.md.
# Meant for both local use and CI
# (.github/workflows/stls-core-node-interop.yml calls it).
#
# Assumes (from the repo root):
#   - node/stls-core: npm install already run, npm run build already run
#   - python/stls-core/.venv: pip install -e ".[dev,pqc]" already run
#
# Environment variables:
#   PYTHON_VENV          path to the venv's python (default: python/stls-core/.venv/bin/python)
#   TEST_REDIS_URL        URL of an already-reachable Redis (default: redis://localhost:6399)
#   START_LOCAL_REDIS     if "1", start/stop a local redis-server on TEST_REDIS_URL's port
#                         (handy locally; CI uses a service container instead, so "0")
#   SKIP_PQC              if "1", skip the PQC tests (require liboqs installed)
#   SKIP_ARQIT_SIDECAR    if "1", skip the Arqit sidecar tests (both the RPC-mechanics
#                         smoke test and the role guard check)
#   AUTH_PG_DSN            if set, also run the live PostgresLoader tests against
#                         this DSN (auto-detected — no SKIP_ flag needed to opt in)
#   ARQIT_PY_CLIENT_CONFIG / ARQIT_PY_SERVER_CONFIG
#                         if both set (to a real, already-provisioned Arqit device
#                         pair's JSON configs), also run the real arqit_py peering
#                         test through two separate sidecar processes (auto-detected,
#                         like AUTH_PG_DSN — no flag needed to opt in). Requires the
#                         [arqit_py] extra installed in the Python venv. See
#                         tests/interop/arqit-sidecar-real.live.ts for the full
#                         rationale (python/stls-core/sidecar/ARQIT_PEERING_NOTES.md
#                         has the underlying investigation) and the cwd caveat about
#                         relative paths inside those device JSON configs.
#
# Typical local usage (no redis-server already running):
#   START_LOCAL_REDIS=1 ./tests/run-interop-tests.sh
#
# CI usage (Redis already provided as a service container on TEST_REDIS_URL):
#   TEST_REDIS_URL=redis://localhost:6399 ./tests/run-interop-tests.sh

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."  # node/stls-core/
REPO_ROOT="$(cd ../.. && pwd)"

PYTHON_VENV="${PYTHON_VENV:-$REPO_ROOT/python/stls-core/.venv/bin/python}"
export TEST_REDIS_URL="${TEST_REDIS_URL:-redis://localhost:6399}"
REDIS_PORT="$(echo "$TEST_REDIS_URL" | sed -E 's#.*:([0-9]+)/?$#\1#')"
START_LOCAL_REDIS="${START_LOCAL_REDIS:-0}"
SKIP_PQC="${SKIP_PQC:-0}"
SKIP_ARQIT_SIDECAR="${SKIP_ARQIT_SIDECAR:-0}"

SIDECAR_SOCKET="/tmp/stls-arqit-sidecar-ci-$$.sock"
SIDECAR_SOCKET_SERVER="/tmp/stls-arqit-sidecar-server-ci-$$.sock"
SIDECAR_SOCKET_CLIENT="/tmp/stls-arqit-sidecar-client-ci-$$.sock"
# Separate pair for step [11/11]: each sidecar process can only complete
# its SERVER-role "start" ONCE per process lifetime (DevicePolicyStore is
# a process-wide singleton in the Arqit SDK — see ARQIT_PEERING_NOTES.md),
# so step [10/11]'s pair can't be reused here.
SIDECAR_SOCKET_SERVER2="/tmp/stls-arqit-sidecar-server2-ci-$$.sock"
SIDECAR_SOCKET_CLIENT2="/tmp/stls-arqit-sidecar-client2-ci-$$.sock"
STARTED_LOCAL_REDIS=0

cleanup() {
  if [ "$STARTED_LOCAL_REDIS" = "1" ]; then
    redis-cli -p "$REDIS_PORT" shutdown nosave >/dev/null 2>&1 || true
  fi
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET" >/dev/null 2>&1 || true
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET_SERVER" >/dev/null 2>&1 || true
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET_CLIENT" >/dev/null 2>&1 || true
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET_SERVER2" >/dev/null 2>&1 || true
  pkill -f "arqit_sidecar.py.*$SIDECAR_SOCKET_CLIENT2" >/dev/null 2>&1 || true
  rm -f "$SIDECAR_SOCKET" "$SIDECAR_SOCKET_SERVER" "$SIDECAR_SOCKET_CLIENT" "$SIDECAR_SOCKET_SERVER2" "$SIDECAR_SOCKET_CLIENT2"
  rm -f tests/interop/node-produced-envelope.json tests/interop/pqc-vector.json tests/interop/pqc-vector-response.json
  rm -f tests/interop/pqc-handshake-*.json
}
trap cleanup EXIT

echo "== [1/11] auth-related error-path smoke (PostgresLoader, no real DB) =="
npx tsx tests/postgres-loader.smoke.ts

echo "== [2/11] Provider factory (KeyProviderFactory/KeySessionProviderFactory, real handshake, base + pqc) =="
npx tsx tests/interop/provider-factory.smoke.ts

if [ "$START_LOCAL_REDIS" = "1" ]; then
  echo "== starting a local redis-server on port $REDIS_PORT =="
  redis-server --port "$REDIS_PORT" --daemonize yes --save ""
  STARTED_LOCAL_REDIS=1
  sleep 0.5
fi

echo "== [3/11] crypto interop (Node decrypts a Python envelope and vice versa) =="
npx tsx tests/interop/crypto.smoke.ts
"$PYTHON_VENV" tests/interop/python_crypto_verify_node_envelope.py
rm -f tests/interop/node-produced-envelope.json

if [ "$SKIP_PQC" != "1" ]; then
  echo "== [4/11] PQC interop (X25519 + ML-KEM-768, Node <-> liboqs-python) =="
  npx tsx tests/interop/pqc-step1-node-keygen.ts
  "$PYTHON_VENV" tests/interop/python_pqc_server_step.py
  npx tsx tests/interop/pqc-step2-node-verify.ts
  rm -f tests/interop/pqc-vector.json tests/interop/pqc-vector-response.json

  echo "== [4b/11] PQC handshake orchestration (performPqcHandshake end-to-end, not just raw KEM/DH) =="
  rm -f tests/interop/pqc-handshake-*.json
  npx tsx tests/interop/pqc-handshake-step1-node-request.ts
  "$PYTHON_VENV" tests/interop/python_pqc_handshake_step2_server.py &
  PQC_SERVER_PID=$!
  sleep 1
  npx tsx tests/interop/pqc-handshake-step3-node-verify.ts
  wait "$PQC_SERVER_PID"
  rm -f tests/interop/pqc-handshake-*.json
else
  echo "== [4/11] PQC interop SKIPPED (SKIP_PQC=1) =="
fi

echo "== [5/11] Redis session cache interop (bidirectional read/write) =="
npx tsx tests/interop/redis-step1-node-put.ts
"$PYTHON_VENV" tests/interop/python_redis_step2_read_and_put.py
npx tsx tests/interop/redis-step3-node-get.ts

echo "== [6/11] Auth module (ApiKeyManager + a real RedisKeyCache) =="
npx tsx tests/auth.smoke.ts

echo "== [7/11] BackgroundRefresher distributed lock (real Redis, scheduled refresh) =="
npx tsx tests/background-refresher.live.ts

if [ -n "${AUTH_PG_DSN:-}" ]; then
  echo "== [8/11] PostgresLoader live (real PostgreSQL via AUTH_PG_DSN) =="
  npx tsx tests/postgres-loader.live.ts
  npx tsx tests/apikeymanager-postgres.live.ts
else
  echo "== [8/11] PostgresLoader live SKIPPED (AUTH_PG_DSN not set) =="
fi

if [ "$SKIP_ARQIT_SIDECAR" != "1" ]; then
  echo "== [9/11] Arqit sidecar (JSON-RPC over a Unix socket, provider_name=base, plus the role guard) =="
  rm -f "$SIDECAR_SOCKET"
  KEY_PROVIDER_NAME=base "$PYTHON_VENV" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" \
    --socket "$SIDECAR_SOCKET" &
  SIDECAR_PID=$!
  sleep 1
  ARQIT_SIDECAR_SOCKET="$SIDECAR_SOCKET" npx tsx tests/interop/arqit-sidecar.smoke.ts
  # Same already-running sidecar process, still provider_name=base for
  # the "start" calls — the guard only applies to provider_name=arqit_py
  # (see arqit-sidecar-guard.smoke.ts), but it needs its OWN process
  # (any prior role claim on this one is for "base", not "arqit_py", so
  # it wouldn't exercise the real guard path) — reuse would also hit the
  # unrelated DevicePolicyStore-singleton issue documented in
  # ARQIT_PEERING_NOTES.md if it ever got to a real "arqit_py" start.
  kill "$SIDECAR_PID" 2>/dev/null || true
  wait "$SIDECAR_PID" 2>/dev/null || true

  rm -f "$SIDECAR_SOCKET"
  "$PYTHON_VENV" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET" &
  SIDECAR_PID=$!
  sleep 1
  ARQIT_SIDECAR_SOCKET="$SIDECAR_SOCKET" npx tsx tests/interop/arqit-sidecar-guard.smoke.ts
  kill "$SIDECAR_PID" 2>/dev/null || true
  wait "$SIDECAR_PID" 2>/dev/null || true
else
  echo "== [9/11] Arqit sidecar SKIPPED (SKIP_ARQIT_SIDECAR=1) =="
fi

if [ "$SKIP_ARQIT_SIDECAR" != "1" ] && [ -n "${ARQIT_PY_CLIENT_CONFIG:-}" ] && [ -n "${ARQIT_PY_SERVER_CONFIG:-}" ]; then
  echo "== [10/11] Arqit real peering (provider_name=arqit_py, two separate sidecar processes) =="
  rm -f "$SIDECAR_SOCKET_SERVER" "$SIDECAR_SOCKET_CLIENT"
  "$PYTHON_VENV" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET_SERVER" &
  SERVER_SIDECAR_PID=$!
  "$PYTHON_VENV" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET_CLIENT" &
  CLIENT_SIDECAR_PID=$!
  sleep 1
  ARQIT_SIDECAR_SOCKET_SERVER="$SIDECAR_SOCKET_SERVER" \
    ARQIT_SIDECAR_SOCKET_CLIENT="$SIDECAR_SOCKET_CLIENT" \
    npx tsx tests/interop/arqit-sidecar-real.live.ts
  kill "$SERVER_SIDECAR_PID" "$CLIENT_SIDECAR_PID" 2>/dev/null || true
  wait "$SERVER_SIDECAR_PID" "$CLIENT_SIDECAR_PID" 2>/dev/null || true
else
  echo "== [10/11] Arqit real peering SKIPPED (ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG not set, or SKIP_ARQIT_SIDECAR=1) =="
fi

if [ "$SKIP_ARQIT_SIDECAR" != "1" ] && [ -n "${ARQIT_PY_CLIENT_CONFIG:-}" ] && [ -n "${ARQIT_PY_SERVER_CONFIG:-}" ]; then
  echo "== [11/11] Arqit real peering through KeySessionProviderFactory (not just the sidecar classes directly) =="
  rm -f "$SIDECAR_SOCKET_SERVER2" "$SIDECAR_SOCKET_CLIENT2"
  "$PYTHON_VENV" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET_SERVER2" &
  SERVER_SIDECAR_PID2=$!
  "$PYTHON_VENV" "$REPO_ROOT/python/stls-core/sidecar/arqit_sidecar.py" --socket "$SIDECAR_SOCKET_CLIENT2" &
  CLIENT_SIDECAR_PID2=$!
  sleep 1
  ARQIT_SIDECAR_SOCKET_SERVER="$SIDECAR_SOCKET_SERVER2" \
    ARQIT_SIDECAR_SOCKET_CLIENT="$SIDECAR_SOCKET_CLIENT2" \
    npx tsx tests/interop/provider-factory-arqit-real.live.ts
  kill "$SERVER_SIDECAR_PID2" "$CLIENT_SIDECAR_PID2" 2>/dev/null || true
  wait "$SERVER_SIDECAR_PID2" "$CLIENT_SIDECAR_PID2" 2>/dev/null || true
else
  echo "== [11/11] Arqit real peering via factory SKIPPED (ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG not set, or SKIP_ARQIT_SIDECAR=1) =="
fi

echo "== ALL INTEROPERABILITY TESTS PASSED =="
