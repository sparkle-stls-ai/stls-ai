"""
key_providers_test.py
=====================
Test of the KeyProvider arqit_py with:
  - parallel key provisioning (N simultaneous sessions, same device_id)
  - streaming SecureA2A calls on each session with distinct messages
  - 24 total calls in groups of 6 (4 rounds)

Usage:
    python key_providers_test.py

Expected environment variables:
    KEY_PROVIDER_NAME=arqit_py
    ARQIT_PY_CLIENT_CONFIG=/path/to/client_config.json
    ARQIT_PY_TRANSPORT=socket
    BASE_URL=http://localhost:8001          (default)
    AGENT_PATH=/agents/adk-agent           (default)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx

from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    SecureA2ATransportAdapter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_URL          = os.getenv("BASE_URL",          "http://localhost:8001")
AGENT_PATH        = os.getenv("AGENT_PATH",        "/agents/adk-agent")
KEY_PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "arqit_py").strip().lower()

# 24 distinct messages (ticker + varied questions) — 4 rounds × 6 sessions
MESSAGES = [
    # Round 1
    "What is the current price of AAPL?",
    "Tell me the price of META today.",
    "How is TSLA performing on the market?",
    "What is the current quote for NVDA?",
    "Give me the price of MSFT right now.",
    "What is the current value of GOOGL?",
    # Round 2
    "What is the price of AMZN at this moment?",
    "How is NFLX trading today?",
    "Tell me the price of AMD.",
    "What is the current quote for INTC?",
    "Price of ORCL right now?",
    "How is UBER performing on the market today?",
    # Round 3
    "What is the price of COIN?",
    "Tell me the quote for PYPL.",
    "How is SHOP doing today?",
    "What is the current price of SQ?",
    "Give me the price of SPOT.",
    "Quote for SNAP right now?",
    # Round 4
    "What is the price of TWLO?",
    "How is DDOG trading today?",
    "Tell me the price of SNOW.",
    "What is the current quote for CRWD?",
    "Current price of ZS?",
    "How is NET performing on the market right now?",
]

GROUP_SIZE     = 6
NUM_GROUPS     = len(MESSAGES) // GROUP_SIZE   # 4
STREAM_TIMEOUT = 120.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ok(label: str):
    print(f"  ✅ {label}")

def _fail(label: str, detail: str = ""):
    print(f"  ❌ {label}{': ' + detail if detail else ''}")

def _warn(label: str):
    print(f"  ⚠️  {label}")


# ── Step 1: key provisioning ───────────────────────────────────────────────────

async def open_session(session_num: int) -> tuple[str, bytes]:
    """
    Negotiates a key for this session. Returns (kid, kdata).

    For arqit_py: provisioning via SKA-Platform (kid = Arqit key identifier).
    For pqc:      KEM handshake X25519 + ML-KEM-768 (kid = UUID from server).
                  NOTE: with SESSION_CACHE_BACKEND=memory the session is valid
                  only for the first agent registered in the server.
                  For multi-agent use SESSION_CACHE_BACKEND=redis.
    """
    t0 = time.monotonic()

    if KEY_PROVIDER_NAME == "pqc":
        from stls_a2a import create_pqc_a2a_transport
        adapter = await create_pqc_a2a_transport(
            BASE_URL,
            client_id=f"pqc-stress-{session_num:03d}",
        )
        kid   = adapter.session_id
        kdata = adapter.transport.export_session_key()
    else:
        factory  = KeySessionProviderFactory()
        provider = factory.get_session_provider(
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


# ── Step 2: secure streaming call ─────────────────────────────────────────────

async def stream_call(
    session_num: int,
    kid: str,
    kdata: bytes,
    message_text: str,
) -> dict[str, Any]:
    """Calls the agent in streaming mode with the negotiated key."""
    adapter = SecureA2ATransportAdapter(
        session_key=kdata,
        session_id=kid,
        base_url=BASE_URL + AGENT_PATH,
    )

    message = {
        "jsonrpc": "2.0",
        "method":  "messages/sendStream",
        "params": {
            "contextId": f"ctx-stress-{session_num:03d}",
            "message": {
                "role":  "user",
                "parts": [{"type": "text", "text": message_text}],
            },
        },
        "id": session_num,
    }

    text_chunks:   list[str]  = []
    status_events: list[str]  = []
    first_chunk_t: float | None = None
    error:         dict | None  = None
    t0 = time.monotonic()

    try:
        async for chunk in adapter.stream_secure_message(
            message, stream_timeout=STREAM_TIMEOUT
        ):
            event = chunk.get("event", "")

            if event == "error":
                error = chunk
                break

            elif event == "TaskArtifactUpdateEvent":
                if first_chunk_t is None:
                    first_chunk_t = time.monotonic() - t0
                for part in chunk.get("parts", []):
                    if part.get("type") == "text" and part.get("text"):
                        text_chunks.append(part["text"])

            elif event == "TaskStatusUpdateEvent":
                state = chunk.get("status", "")
                if state:
                    status_events.append(state)

    except Exception as exc:
        error = {"message": str(exc), "code": -1}

    elapsed   = time.monotonic() - t0
    full_text = "".join(text_chunks)

    return {
        "session_num":   session_num,
        "kid":           kid,
        "message":       message_text,
        "text":          full_text,
        "status_events": status_events,
        "first_chunk_t": first_chunk_t,
        "elapsed":       elapsed,
        "error":         error,
    }


# ── Step 3: full session (provisioning + stream) ───────────────────────────────

async def run_session(session_num: int, message_text: str) -> dict[str, Any]:
    """Key provisioning + streaming in a single coroutine."""
    try:
        kid, kdata = await open_session(session_num)
        result = await stream_call(session_num, kid, kdata, message_text)
        result["kdata"] = kdata   # keep raw bytes for key summary
        return result
    except Exception as exc:
        logger.error("Session-%02d failed: %s", session_num, exc, exc_info=True)
        return {
            "session_num":   session_num,
            "kid":           None,
            "kdata":         None,
            "message":       message_text,
            "text":          "",
            "status_events": [],
            "first_chunk_t": None,
            "elapsed":       0.0,
            "error":         {"message": str(exc), "code": -1},
        }


# ── Test A: key isolation (2 sessions) ────────────────────────────────────────

async def test_key_isolation():
    print("\n" + "="*70)
    print("Test A: Key isolation (2 simultaneous sessions)")
    print("="*70)

    (kid1, k1), (kid2, k2) = await asyncio.gather(
        open_session(1),
        open_session(2),
    )

    _ok(f"kid1={kid1[:16]}…") if kid1 != kid2 else _fail("Identical kids", kid1)
    _ok("Keys are different ✓") if k1 != k2 else _fail("Identical keys")

    # Key listing for Test A
    print(f"\n── Keys for Test A")
    print(f"  {'Session':<12} {'kid':<40} {'key (base64)':<50} {'len':>4}")
    print(f"  {'-'*12} {'-'*40} {'-'*50} {'-'*4}")
    for num, kid, kdata in [(1, kid1, k1), (2, kid2, k2)]:
        kb64 = base64.b64encode(kdata).decode()
        print(f"  Session-{num:02d}   {kid:<40} {kb64:<50} {len(kdata):>4}")


# ── Test B: stress — 24 calls in groups of 6 ──────────────────────────────────

async def test_stress():
    print("\n" + "="*70)
    print(f"Test B: Stress — {NUM_GROUPS} rounds × {GROUP_SIZE} simultaneous sessions  ({NUM_GROUPS*GROUP_SIZE} total)")
    print(f"        Agent: {BASE_URL}{AGENT_PATH}")
    print("="*70)

    all_results: list[dict] = []
    session_counter = 0

    for round_num in range(1, NUM_GROUPS + 1):
        start_idx  = (round_num - 1) * GROUP_SIZE
        group_msgs = MESSAGES[start_idx : start_idx + GROUP_SIZE]

        print(f"\n── Round {round_num}/{NUM_GROUPS}  "
              f"(sessions {session_counter+1}–{session_counter+GROUP_SIZE})")

        t_round = time.monotonic()
        group_results = await asyncio.gather(*[
            run_session(session_counter + i + 1, msg)
            for i, msg in enumerate(group_msgs)
        ])
        round_elapsed = time.monotonic() - t_round

        kids_in_round: set[str] = set()

        for r in group_results:
            n   = r["session_num"]
            err = r["error"]

            if err:
                _fail(f"Session-{n:02d}", f"{err.get('message','?')[:80]}")
                continue

            kid      = r["kid"] or ""
            text     = r["text"]
            statuses = r["status_events"]
            elapsed  = r["elapsed"]
            first_t  = r["first_chunk_t"]

            collision  = kid in kids_in_round
            kids_in_round.add(kid)
            status_ok  = "completed" in statuses
            text_ok    = bool(text.strip())

            line = (
                f"Session-{n:02d}  kid={kid[:12]}…  "
                f"statuses={statuses}  "
                f"chars={len(text)}  elapsed={elapsed:.1f}s"
                + (f"  first={first_t:.2f}s" if first_t else "")
            )

            if collision:
                _fail(f"Session-{n:02d} KID COLLISION", kid)
            elif status_ok and text_ok:
                _ok(line)
            elif not status_ok:
                _warn(f"{line}  ← 'completed' missing")
            else:
                _warn(f"{line}  ← empty response")

            print(f"    📤 {r['message']}")
            print(f"    📥 {text[:100]}{'…' if len(text)>100 else ''}")

        print(f"  ⏱  Round {round_num} completed in {round_elapsed:.1f}s")
        all_results.extend(group_results)
        session_counter += GROUP_SIZE

        if round_num < NUM_GROUPS:
            print("  ⏸  Pausing 2s between rounds...")
            await asyncio.sleep(2.0)

    # ── Global summary ─────────────────────────────────────────────────────────
    print("\n" + "="*70)
    print("Test B — Summary")
    print("="*70)

    total     = len(all_results)
    errors    = [r for r in all_results if r["error"]]
    completed = [r for r in all_results if not r["error"] and "completed" in r["status_events"]]
    no_text   = [r for r in all_results if not r["error"] and not r["text"].strip()]

    print(f"  Total sessions  : {total}")
    print(f"  ✅ Completed    : {len(completed)}")
    print(f"  ❌ Errors       : {len(errors)}")
    print(f"  ⚠️  Empty text   : {len(no_text)}")

    elapsed_vals = [r["elapsed"] for r in all_results if not r["error"]]
    if elapsed_vals:
        print(f"  ⏱  Avg elapsed  : {sum(elapsed_vals)/len(elapsed_vals):.1f}s  "
              f"min={min(elapsed_vals):.1f}s  max={max(elapsed_vals):.1f}s")

    all_kids    = [r["kid"] for r in all_results if r["kid"]]
    unique_kids = set(all_kids)
    if len(unique_kids) == len(all_kids):
        _ok(f"All kids unique ({len(unique_kids)}/{total})")
    else:
        _fail("Duplicate kids found", f"total={len(all_kids)}  unique={len(unique_kids)}")

    # ── Per-session key listing ────────────────────────────────────────────────
    print("\n── Keys used per session")
    print(f"  {'Session':<12} {'kid':<40} {'key (base64)':<50} {'len':>4}")
    print(f"  {'-'*12} {'-'*40} {'-'*50} {'-'*4}")
    for r in all_results:
        n     = r["session_num"]
        kid   = r.get("kid") or "—"
        kdata = r.get("kdata")
        if kdata:
            kb64 = base64.b64encode(kdata).decode()
            klen = len(kdata)
        else:
            kb64 = "—"
            klen = 0
        err_mark = " ❌" if r.get("error") else ""
        print(f"  Session-{n:02d}   {kid:<40} {kb64:<50} {klen:>4}{err_mark}")

    print()
    if not errors and not no_text and len(unique_kids) == len(all_kids):
        print("  🎉 Test B passed — provider stable across 24 sessions × 1 device")
    else:
        print("  ⚠️  Test B completed with anomalies — check the logs above")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    print("="*70)
    print(f"key_providers_test.py  —  {KEY_PROVIDER_NAME} stress test")
    print("="*70)
    print(f"  KEY_PROVIDER_NAME = {KEY_PROVIDER_NAME}")
    print(f"  BASE_URL          = {BASE_URL}")
    print(f"  AGENT_PATH        = {AGENT_PATH}")
    print(f"  GROUP_SIZE × N    = {GROUP_SIZE} × {NUM_GROUPS} = {GROUP_SIZE*NUM_GROUPS} sessions")
    if KEY_PROVIDER_NAME == "pqc":
        print(f"  NOTE: multi-agent PQC requires SESSION_CACHE_BACKEND=redis")

    await test_key_isolation()
    await test_stress()


if __name__ == "__main__":
    asyncio.run(main())