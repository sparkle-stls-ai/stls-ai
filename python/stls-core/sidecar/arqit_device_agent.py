"""
arqit_device_agent.py
-----------------------
Standalone process whose only job is to own an arqit_py device
identity (authenticate + negotiate against the real Arqit cloud) and
feed the resulting session keys into the shared Redis-backed key
store (KEY_STORE_BACKEND=redis) — see issue #38.

Unlike arqit_sidecar.py (a generic JSON-RPC provider proxy for the
Node port), this is NOT an RPC server: nothing talks to this process
directly. Any number of ARQIT_PY_DEVICE_ROLE=consumer replicas
(Python's own KeySessionProviderFactory, or a Node sidecar pointed at
the same Redis namespace) read the keys this process negotiates
purely through Redis — see
stls_core_providers_keystore.ArqitKeyStoreFactory and this issue's
device-level owner lock (build_owner_lock_store()).

This process IS the "owner" in ARQIT_PY_DEVICE_ROLE terms — it always
calls KeyProviderServerArqitPy.start() regardless of
ARQIT_PY_DEVICE_ROLE's own value. That env var is read by
KeySessionProviderFactory for CONSUMERS deciding whether to skip
start(); this script never goes through that factory method at all,
it builds the provider directly, so ARQIT_PY_DEVICE_ROLE is irrelevant
to it one way or the other.

Exposes two plain HTTP endpoints (stdlib http.server, no new
dependency) for Kubernetes probes:
    GET /healthz  -> 200 once start() has completed successfully,
                     503 before that or if it failed (gates readiness
                     — don't route traffic here until this is 200)
    GET /livez    -> 200 as long as this process is running at all
                     (gates restarts — a failed start() won't be fixed
                     by one, so this stays 200 even then; only readiness
                     should block traffic)

NOT yet validated against a real Arqit device/cloud — see this
directory's own test_arqit_device_agent.py for what IS covered
(argument parsing, health endpoints, the start()-failure path, all
with the real provider mocked out). Validate manually against your
own device before trusting this in production, same caution as every
other real arqit_py path in this project (see ARQIT_PEERING_NOTES.md).

Usage:
    ARQIT_PY_SERVER_CONFIG=/path/to/device-server.json \
    KEY_STORE_BACKEND=redis \
    KEY_STORE_REDIS_URL=redis://localhost:6379 \
    KEY_STORE_REDIS_NAMESPACE=my-device \
    python arqit_device_agent.py [--health-port 8080]

Environment variables:
    ARQIT_PY_SERVER_CONFIG     path to the device's server config JSON
                               (same as
                               KeyProviderArqitPy.create_provider_server(),
                               which this script calls directly)
    ARQIT_PY_TRANSPORT         "socket" (default) | "mqtt"
    KEY_STORE_BACKEND          must be "redis" for any
                               ARQIT_PY_DEVICE_ROLE=consumer replica
                               elsewhere to see these keys at all — see
                               stls_core_providers_keystore.py
    DEVICE_AGENT_HEALTH_PORT   health endpoint port (default: 8080),
                               overridden by --health-port
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


class HealthState:
    """Shared between the health HTTP server and _run()'s own lifecycle."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.failed = threading.Event()


def make_health_handler(state: HealthState) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            logger.debug("[health] " + format, *args)

        def do_GET(self) -> None:
            if self.path == "/livez":
                # Alive = this process is still serving this request at
                # all. A failed start() still means "alive" in the K8s
                # liveness sense (restarting won't fix a bad config) —
                # /healthz is what should gate traffic, not this.
                self.send_response(200)
                self.end_headers()
                return
            if self.path == "/healthz":
                if state.started.is_set() and not state.failed.is_set():
                    self.send_response(200)
                else:
                    self.send_response(503)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    return Handler


async def run(health_port: int) -> None:
    from stls_core.providers.stls_core_providers_arqit_py import KeyProviderArqitPy

    state = HealthState()
    health_server = ThreadingHTTPServer(("0.0.0.0", health_port), make_health_handler(state))
    health_thread = threading.Thread(
        target=health_server.serve_forever, daemon=True, name="device-agent-health",
    )
    health_thread.start()
    logger.info("[device-agent] health endpoints on :%d (/healthz, /livez)", health_port)

    provider = KeyProviderArqitPy().create_provider_server(port=0, options={})

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(__import__("signal"), sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                # add_signal_handler isn't available on every platform
                # (e.g. Windows) — this script targets Linux containers,
                # same as the rest of this project's K8s-facing pieces.
                pass

    try:
        await provider.start()
    except Exception:
        state.failed.set()
        logger.exception("[device-agent] start() failed — /healthz will stay 503")
        raise
    state.started.set()
    logger.info("[device-agent] device started and listening — readiness is now OK")

    await stop_event.wait()
    logger.info("[device-agent] signal received, stopping provider")
    await provider.stop()
    health_server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--health-port",
        type=int,
        default=int(os.getenv("DEVICE_AGENT_HEALTH_PORT", "8080")),
    )
    args = parser.parse_args()
    asyncio.run(run(args.health_port))


if __name__ == "__main__":
    main()
