"""
test_arqit_py_same_device_concurrent_real.py
------------------------------------------------
Controlled, pure-Python reproduction of the failure first observed via
the Node sidecar path (samples/client/12_client_mcp_ms/client_multi_session.ts,
arqit_py, 2026-10-23): N CONCURRENT client sessions for the SAME
arqit_py device identity.

Every earlier "concurrent" test of this kind in this project
(samples/client/10_client_ms/client_multi_session.py,
samples/client/11_client_mcp_ms/client_multi_session.py, and the
original version of test_arqit_py_concurrent_devices_real.py) called
KeyProviderClientArqitPy.start() — a plain, blocking, SYNCHRONOUS
method — directly from a coroutine inside asyncio.gather(). Since
asyncio only switches coroutines at `await` points, and start() has
none before the sensitive DeviceMetadata.get_instance() call inside
it returns, those "concurrent" sessions were silently serialized by
Python's own cooperative scheduler — never actually racing. This file
fixes that: `asyncio.to_thread()` dispatches start() to a real OS
thread, exactly mirroring what arqit_sidecar.py's
`loop.run_in_executor()` does for each RPC connection — which is why
the Node path caught something this project's pure-Python tests
never could.

Uses DEDICATED, THROWAWAY devices (sf-race-throwaway-{server,client},
see _arqit_race_fixtures/) provisioned specifically for this test —
never any device used by another sample in this project — so a
corrupted ratchet here costs nothing else. Still a real call against
the real Arqit cloud; don't run this back-to-back repeatedly (see
sidecar/ARQIT_PEERING_NOTES.md's rate-limiting note).

Usage:
    python tests/test_arqit_py_same_device_concurrent_real.py [N]

    N = number of concurrent sessions (default 3, matching the
    original Node failure's group size).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).parent / "_arqit_race_fixtures"
CLIENT_CONFIG = str(FIXTURES_DIR / "aq-race-throwaway-client.json")
SERVER_ROLE_SCRIPT = FIXTURES_DIR / "race_test_server_role.py"

# DEVICE_METADATA_FILEPATH etc. in the device configs are relative
# ("./aq-race-throwaway-client_metadata.json") and resolve against the
# process's cwd, not the config file's own directory — the server
# subprocess gets this right via subprocess.Popen(cwd=FIXTURES_DIR),
# but this (main) process also needs it for the client-side calls
# below. See sidecar/ARQIT_PEERING_NOTES.md for the same pitfall
# documented elsewhere. Missing this previously caused a fast,
# unrelated FileNotFoundError/AttributeError that was then mistaken
# for the DeviceMetadata race itself (the SDK's own class-level
# AuthenticationClient.__mutex has no try/finally around the call that
# threw, so that unrelated exception leaked the mutex for the rest of
# the process, hanging every subsequent session regardless of this
# test's own locking).
os.chdir(FIXTURES_DIR)


def _start_server_subprocess() -> subprocess.Popen:
    """
    Starts the SERVER role in its own OS process (required — see
    sidecar/ARQIT_PEERING_NOTES.md) and waits for its "READY" line.
    """
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_ROLE_SCRIPT)],
        cwd=str(FIXTURES_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + 30
    lines: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("server subprocess exited early:\n" + "".join(lines))
            continue
        lines.append(line)
        print("[server] " + line, end="")
        if line.strip() == "READY":
            return proc
    proc.kill()
    raise TimeoutError("server subprocess did not become ready within 30s:\n" + "".join(lines))


async def _resolve_one_session(session_num: int) -> dict:
    from stls_core.providers.stls_core_providers_arqit_py import KeyProviderArqitPy

    t0 = time.monotonic()
    try:
        provider = KeyProviderArqitPy().create_provider_client(
            host="", port=0, options={"config_path": CLIENT_CONFIG},
        )
        # THE fix vs every earlier "concurrent" test in this project:
        # dispatch the blocking start() to a real OS thread, exactly
        # like arqit_sidecar.py's loop.run_in_executor() does per RPC
        # connection. Without this, these N "concurrent" sessions
        # would be silently serialized by asyncio and never race.
        await asyncio.to_thread(provider.start)
        session = provider.createProviderSession()
        kid = await session.getSessionKeyIdentifier()
        kdata = await session.getSessionKeyData(kid)
        elapsed = time.monotonic() - t0
        logger.info("Session-%d OK kid=%s elapsed=%.2fs", session_num, kid, elapsed)
        return {"session_num": session_num, "kid": kid, "kdata": kdata, "error": None}
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.error("Session-%d FAILED after %.2fs: %s", session_num, elapsed, exc, exc_info=True)
        return {"session_num": session_num, "kid": None, "kdata": None, "error": exc}


async def _run(n: int) -> None:
    print(f"\n=== Same-device concurrent race test (pure Python, real threads) — N={n} ===\n")
    results = await asyncio.gather(*[_resolve_one_session(i + 1) for i in range(n)])

    errors = [r for r in results if r["error"]]
    kids = [r["kid"] for r in results if r["kid"]]
    unique_kids = set(kids)

    print("\n--- Results ---")
    for r in results:
        if r["error"]:
            print(f"  Session-{r['session_num']}: FAILED — {r['error']}")
        else:
            print(f"  Session-{r['session_num']}: OK kid={r['kid']}")

    print(f"\nTotal: {n}  Errors: {len(errors)}  Unique kids: {len(unique_kids)}/{len(kids)}")
    if errors or len(unique_kids) != len(kids):
        print("\nRESULT: ANOMALIES FOUND (errors and/or duplicate kids) — consistent with a real race")
    else:
        print("\nRESULT: no anomalies this run — does NOT prove the race doesn't exist (see module docstring); "
              "run again on a different day before trusting a clean result, same caution as "
              "sidecar/ARQIT_PEERING_NOTES.md's rate-limiting note.")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    print("Starting throwaway SERVER role subprocess...")
    server_proc = _start_server_subprocess()
    try:
        asyncio.run(_run(n))
    finally:
        print("\nStopping server subprocess...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()


if __name__ == "__main__":
    main()
