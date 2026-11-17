"""
replica_server_role.py
------------------------
Standalone, parameterized arqit_py SERVER-role harness used ONLY by
../test_arqit_py_redis_replicas_real.py to simulate ONE
horizontally-scaled replica (one OS process) serving the SAME Arqit
device identity as its siblings, all sharing one Redis-backed
key-store namespace (see
stls_core_providers_keystore.ArqitKeyStoreFactory.build(role="server")).

Unlike ../_arqit_race_fixtures/race_test_server_role.py (one fixed
server, no cross-process coordination), this script also accepts
simple line commands on stdin so the orchestrating test can trigger a
no-hint getSessionKeyIdentifier() call on demand, independently on
each replica, to probe whether two sibling replicas can be handed the
SAME kid by the shared (non-atomic — see
ArqitKeyStoreBase.wait_for_new_key()) pending ZSET.

Never run directly — spawned by test_arqit_py_redis_replicas_real.py,
which sets KEY_STORE_BACKEND=redis and friends in this process's
environment before launch.

Usage:
    python replica_server_role.py <config_path> <label>

Protocol (stdin -> stdout):
    (on startup, once listening) -> "READY <label>"
    "RESOLVE\n"                  -> "KID <label> <kid>"  or  "KIDERR <label> <message>"
    "QUIT\n" or EOF               -> process exits
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)


async def main() -> None:
    if len(sys.argv) < 3:
        print("usage: replica_server_role.py <config_path> <label>", file=sys.stderr)
        sys.exit(2)
    config_path, label = sys.argv[1], sys.argv[2]

    from stls_core.providers.stls_core_providers_arqit_py import KeyProviderArqitPy

    provider = KeyProviderArqitPy().create_provider_server(
        port=0, options={"config_path": config_path},
    )
    await provider.start()
    session = provider.createProviderSession()
    print(f"READY {label}", flush=True)

    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            break
        cmd = line.strip()
        if cmd == "QUIT":
            break
        if cmd == "RESOLVE":
            try:
                kid = await asyncio.wait_for(session.getSessionKeyIdentifier(), timeout=45.0)
                print(f"KID {label} {kid}", flush=True)
            except Exception as exc:  # noqa: BLE001 — surfaced to the orchestrator, not swallowed
                print(f"KIDERR {label} {exc}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
