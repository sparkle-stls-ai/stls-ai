"""
client_multi_session.py — N concurrent sessions, 1 device vs 1 server
-------------------------------------------------------------------------
Adapted from python/stls-a2a/tests/key_providers_test.py. Unlike
client_multi_server.py (1 client -> N different servers, with N
different device identities in the same process), this exercises the
other axis: a SINGLE device/client config opening MULTIPLE concurrent
sessions toward a SINGLE server — see the "1:1 architecture (Option A)"
note in stls_core_providers_arqit_py.py: create_provider_client() is
called once per session, and kid/key stay isolated per session even
though they share the same Arqit device.

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py:
- pqc: each session does its own HTTP X25519+ML-KEM-768 handshake
  (genuinely distinct kid/key per session).
- arqit_py: each session authenticates *the same* device and
  negotiates a fresh bilocation+P2P key — distinct kid/key per session
  while remaining the same device identity (expected behavior of the
  provider's "1:1" architecture).
- base: NO negotiation at all — the provider always returns the same
  fixed kid/key (_BASE_KEY_ID/_BASE_KEY_DATA in stls_core). With this
  provider, Test A and Test B will report a "collision" on kid and
  key: this is EXPECTED (shared fixed key, dev/test provider), not a
  defect in the test or the code.

Unlike the original, this uses send_secure_message() (non-streaming):
our minimal ADK agent (agent.py under 10_server_ms/) doesn't advertise
capabilities.streaming in its card, and the point of this test is the
robustness of the key/session layer under concurrency, not SSE
streaming.

Run (after starting ONE of the two server_ms_* servers):
    python client_multi_session.py
"""

from __future__ import annotations

import os

# Anchor the relative Arqit config paths (DEVICE_METADATA_FILEPATH,
# etc.) to this script's own directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import asyncio
import base64
import logging
import time
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    SecureA2ATransportAdapter,
    extract_a2a_text,
)

# ── Configuration ─────────────────────────────────────────────────────────────
# Defaults to server_ms_1 + client-ms-1 (the first of the two already
# provisioned device/server pairs) — for the second one, just override
# BASE_URL/AGENT_PATH/ARQIT_PY_CLIENT_CONFIG in the environment at
# launch time.
KEY_PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()
BASE_URL          = os.getenv("BASE_URL",   "http://localhost:8101")
AGENT_PATH        = os.getenv("AGENT_PATH", "/agents/server-ms-1")

# Smaller scale than the original (24 = 4x6): this is a targeted check,
# not a load test, and for arqit_py every session is a real
# authentication + bilocation/P2P negotiation against the real Arqit
# cloud — better not to burn too many. Raise GROUP_SIZE/NUM_GROUPS if
# you actually want to stress the provider.
GROUP_SIZE = int(os.getenv("GROUP_SIZE", "3"))
NUM_GROUPS = int(os.getenv("NUM_GROUPS", "2"))

MESSAGES = [f"ping session {i:02d}" for i in range(1, GROUP_SIZE * NUM_GROUPS + 1)]


def _ok(label: str) -> None:
    print(f"  OK   {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


def _warn(label: str) -> None:
    print(f"  WARN {label}")


# ── Step 1: open a session (key provisioning) ────────────────────────────────

async def open_session(session_num: int) -> tuple[str, bytes]:
    """
    Negotiates a key for this session. Returns (kid, kdata).

    pqc:    KEM handshake X25519 + ML-KEM-768 (kid = server-side UUID).
            NOTE: with SESSION_CACHE_BACKEND=memory the session is only
            valid for the first agent registered on the server — not
            an issue here since there's a single agent per server
            process.
    others (base/arqit_py): the generic KeySessionProviderFactory.
    """
    t0 = time.monotonic()

    if KEY_PROVIDER_NAME == "pqc":
        from stls_a2a import create_pqc_a2a_transport
        adapter = await create_pqc_a2a_transport(
            BASE_URL,
            client_id=f"pqc-multi-session-{session_num:03d}",
        )
        kid   = adapter.session_id
        kdata = adapter.transport.session_key
    else:
        provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            host=os.getenv("KEY_SESSION_HOST", "localhost"),
            port=int(os.getenv("KEY_SESSION_PORT", "8081")),
            name=KEY_PROVIDER_NAME,
        )
        kid   = await provider.getSessionKeyIdentifier()
        kdata = await provider.getSessionKeyData(kid)

    elapsed = time.monotonic() - t0

    assert kdata,            f"[Session-{session_num}] empty key"
    assert len(kdata) >= 16, f"[Session-{session_num}] key too short: {len(kdata)} bytes"

    logger.info(
        "Session-%02d  kid=%s  key_len=%d bytes  elapsed=%.2fs",
        session_num, kid, len(kdata), elapsed,
    )
    return kid, kdata


# ── Step 2: encrypted call (non-streaming) ───────────────────────────────────

async def call(session_num: int, kid: str, kdata: bytes, message_text: str) -> dict[str, Any]:
    adapter = SecureA2ATransportAdapter(
        session_key=kdata,
        session_id=kid,
        base_url=BASE_URL + AGENT_PATH,
    )
    message = {
        "jsonrpc": "2.0",
        "id": session_num,
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": message_text}],
            }
        },
    }

    t0 = time.monotonic()
    error: dict | None = None
    text = ""
    try:
        response = await adapter.send_secure_message(message)
        text = extract_a2a_text(response) or ""
    except Exception as exc:
        error = {"message": str(exc), "code": -1}
    finally:
        await adapter.client.aclose()

    return {
        "session_num": session_num,
        "kid": kid,
        "message": message_text,
        "text": text,
        "elapsed": time.monotonic() - t0,
        "error": error,
    }


# ── Step 3: full session (provisioning + call) ───────────────────────────────

async def run_session(session_num: int, message_text: str) -> dict[str, Any]:
    try:
        kid, kdata = await open_session(session_num)
        result = await call(session_num, kid, kdata, message_text)
        result["kdata"] = kdata
        return result
    except Exception as exc:
        logger.error("Session-%02d failed: %s", session_num, exc, exc_info=True)
        return {
            "session_num": session_num, "kid": None, "kdata": None,
            "message": message_text, "text": "", "elapsed": 0.0,
            "error": {"message": str(exc), "code": -1},
        }


# ── Test A: key isolation (2 simultaneous sessions) ──────────────────────────

async def test_key_isolation() -> None:
    print("\n" + "=" * 70)
    print("Test A: key isolation (2 simultaneous sessions)")
    print("=" * 70)

    (kid1, k1), (kid2, k2) = await asyncio.gather(open_session(1), open_session(2))

    if KEY_PROVIDER_NAME == "base":
        print("  (provider=base: kid/key are fixed and shared by design — "
              "the 'collision' below is expected, not a defect)")

    _ok(f"kid1={kid1[:16]}…") if kid1 != kid2 else _warn(f"identical kids (expected with base): {kid1}")
    _ok("keys differ") if k1 != k2 else _warn("identical keys (expected with base)")

    print(f"\n-- Test A keys")
    for num, kid, kdata in [(1, kid1, k1), (2, kid2, k2)]:
        print(f"  Session-{num:02d}  kid={kid:<40}  key={base64.b64encode(kdata).decode():<48}  len={len(kdata)}")


# ── Test B: stress — NUM_GROUPS rounds x GROUP_SIZE simultaneous sessions ───

async def test_stress() -> None:
    total_sessions = NUM_GROUPS * GROUP_SIZE
    print("\n" + "=" * 70)
    print(f"Test B: {NUM_GROUPS} rounds x {GROUP_SIZE} simultaneous sessions ({total_sessions} total)")
    print(f"        Server: {BASE_URL}{AGENT_PATH}")
    print("=" * 70)

    all_results: list[dict] = []
    counter = 0

    for round_num in range(1, NUM_GROUPS + 1):
        group_msgs = MESSAGES[counter:counter + GROUP_SIZE]
        print(f"\n-- Round {round_num}/{NUM_GROUPS} (sessions {counter + 1}-{counter + GROUP_SIZE})")

        t_round = time.monotonic()
        group_results = await asyncio.gather(*[
            run_session(counter + i + 1, msg) for i, msg in enumerate(group_msgs)
        ])
        round_elapsed = time.monotonic() - t_round

        seen_kids: set[str] = set()
        for r in group_results:
            n, err = r["session_num"], r["error"]
            if err:
                _fail(f"Session-{n:02d}", str(err.get("message", "?"))[:100])
                continue

            kid = r["kid"] or ""
            collision = kid in seen_kids
            seen_kids.add(kid)
            line = f"Session-{n:02d}  kid={kid[:16]}…  response={r['text'][:60]!r}  elapsed={r['elapsed']:.1f}s"

            if collision and KEY_PROVIDER_NAME != "base":
                _fail(f"Session-{n:02d} DUPLICATE KID", kid)
            elif collision:
                _warn(f"{line}  (duplicate kid — expected with provider=base)")
            elif r["text"].strip():
                _ok(line)
            else:
                _warn(f"{line}  <- empty response")

        print(f"  Round {round_num} completed in {round_elapsed:.1f}s")
        all_results.extend(group_results)
        counter += GROUP_SIZE

        if round_num < NUM_GROUPS:
            await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print("Test B — summary")
    print("=" * 70)

    errors   = [r for r in all_results if r["error"]]
    no_text  = [r for r in all_results if not r["error"] and not r["text"].strip()]
    all_kids = [r["kid"] for r in all_results if r["kid"]]
    unique   = set(all_kids)

    print(f"  Total sessions  : {len(all_results)}")
    print(f"  Errors          : {len(errors)}")
    print(f"  Empty responses : {len(no_text)}")
    if KEY_PROVIDER_NAME == "base":
        print(f"  Unique kids     : {len(unique)}/{len(all_kids)}  (1 kid expected: provider=base)")
    else:
        print(f"  Unique kids     : {len(unique)}/{len(all_kids)}")
        if len(unique) == len(all_kids):
            _ok("all kids are distinct")
        else:
            _fail("duplicate kids found", f"total={len(all_kids)} unique={len(unique)}")

    ok = not errors and not no_text and (KEY_PROVIDER_NAME == "base" or len(unique) == len(all_kids))
    print("\n  RESULT: " + ("PASS" if ok else "ANOMALIES — see above"))


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 70)
    print(f"client_multi_session.py — provider={KEY_PROVIDER_NAME}")
    print("=" * 70)
    print(f"  KEY_PROVIDER_NAME = {KEY_PROVIDER_NAME}")
    print(f"  BASE_URL+AGENT_PATH = {BASE_URL}{AGENT_PATH}")
    print(f"  GROUP_SIZE x NUM_GROUPS = {GROUP_SIZE} x {NUM_GROUPS} = {GROUP_SIZE * NUM_GROUPS} sessions")
    if KEY_PROVIDER_NAME == "pqc":
        print("  NOTE: multi-agent PQC requires SESSION_CACHE_BACKEND=redis")

    await test_key_isolation()
    await test_stress()


if __name__ == "__main__":
    asyncio.run(main())
