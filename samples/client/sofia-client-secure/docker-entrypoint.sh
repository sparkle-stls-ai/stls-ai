#!/bin/sh
# Start both the Python secure transport service and the Node.js Express server.
# The Python service must be ready before Node starts accepting proxy traffic,
# so we wait for it to respond on the health endpoint.

set -e

SECURE_PORT="${SECURE_TRANSPORT_PORT:-8090}"

echo "[entrypoint] Starting Python secure transport service on port $SECURE_PORT ..."
python3 /app/python/secure_transport.py &
PYTHON_PID=$!

# Wait up to 30 s for the Python service to become healthy.
echo "[entrypoint] Waiting for secure transport service ..."
for i in $(seq 1 30); do
  if wget -qO- "http://localhost:$SECURE_PORT/health" >/dev/null 2>&1; then
    echo "[entrypoint] Secure transport service is ready."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "[entrypoint] ERROR: secure transport service did not start in time." >&2
    exit 1
  fi
  sleep 1
done

echo "[entrypoint] Starting Node.js Express server ..."
exec node /app/server/dist/index.js
