"""
client_multi_session.py — N concurrent MCP sessions, 1 device vs 1 server
-----------------------------------------------------------------------------
MCP analogue of samples/client/10_client_ms/client_multi_session.py.
Unlike client_multi_server.py (which validates SecureToolsetManager,
necessarily through ADK since STLSMCPToolset IS an ADK MCPToolset),
this bypasses ADK entirely — mirroring
python/stls-mcp/tests/vault_client.py's
"handshake -> stls_httpx_factory -> streamablehttp_client -> ClientSession"
pattern directly, exactly as client_multi_session.py (A2A) bypasses
ContextualSecureRemoteA2aAgent. No LLM/GOOGLE_API_KEY needed here.

Unlike stls-a2a's lazy base/arqit_py session scheme (client-side kid
*is* the session_id, no server round trip), MCP always does an
explicit server-side handshake that mints a fresh session_id — so
unlike the A2A version of this test, `base` is NOT expected to
collide here: the server mints a new session_id every handshake
regardless of the underlying key being the same fixed one. Confirmed
empirically against the Node port of this same test
(samples/client/12_client_mcp_ms/client_multi_session.ts).

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py.

Run (after starting ONE of the two server_mcp_ms_* servers):
    python client_multi_session.py
"""

from __future__ import annotations

import os

# Anchor the relative Arqit config path (DEVICE_METADATA_FILEPATH,
# etc.) to this script's own directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
import time
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from stls_core import KeySessionProvider, KeySessionProviderFactory
from stls_mcp import perform_client_handshake, perform_pqc_client_handshake, stls_httpx_factory

# ── Configuration ─────────────────────────────────────────────────────────────
# Defaults to server_mcp_ms_1 + client-mcp-ms-1 (the first of the two
# already provisioned device/server pairs) — for the second one,
# override BASE_URL/TOOL_NAME/ARQIT_PY_CLIENT_CONFIG at launch time.
KEY_PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8201")
TOOL_NAME = os.getenv("TOOL_NAME", "ping_server_mcp_ms_1")

# Smaller scale than a real load test — see the sibling stls-a2a
# version's note on arqit_py sessions being real cloud calls.
GROUP_SIZE = int(os.getenv("GROUP_SIZE", "3"))
NUM_GROUPS = int(os.getenv("NUM_GROUPS", "2"))

MESSAGES = [f"ping session {i:02d}" for i in range(1, GROUP_SIZE * NUM_GROUPS + 1)]


def _ok(label: str) -> None:
    print(f"  OK   {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


# ── Step 1: open a session (handshake) ───────────────────────────────────────

async def open_session(session_num: int) -> tuple[str, Any]:
    """
    Returns (session_id, transport). MCP always does a real
    server-side handshake round trip, even for base/arqit_py (unlike
    stls-a2a's lazy scheme) — so session_id is always server-minted,
    never just the client-resolved kid.
    """
    if KEY_PROVIDER_NAME == "pqc":
        session_id, transport = await perform_pqc_client_handshake(
            BASE_URL, client_id=f"pqc-mcp-multi-session-{session_num:03d}",
        )
    else:
        provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            host=os.getenv("KEY_SESSION_HOST", "localhost"),
            port=int(os.getenv("KEY_SESSION_PORT", "8081")),
            name=KEY_PROVIDER_NAME,
        )
        kid = await provider.getSessionKeyIdentifier()
        kdata = await provider.getSessionKeyData(kid)
        session_id, transport = await perform_client_handshake(
            BASE_URL, kdata, client_id=f"{KEY_PROVIDER_NAME}-mcp-multi-session-{session_num:03d}",
            kid=kid,
        )
    return session_id, transport


# ── Step 2: raw MCP tool call (no ADK) ────────────────────────────────────────

async def call(session_num: int, session_id: str, transport: Any, message_text: str) -> dict[str, Any]:
    t0 = time.monotonic()
    error: dict | None = None
    text = ""
    try:
        factory = stls_httpx_factory(session_id, transport)
        async with streamablehttp_client(url=f"{BASE_URL}/mcp-stls", httpx_client_factory=factory) as (
            read_stream, write_stream, _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(TOOL_NAME, {"message": message_text})
                text = result.content[0].text if result.content else ""
    except Exception as exc:
        error = {"message": str(exc)}

    return {
        "session_num": session_num, "session_id": session_id, "message": message_text,
        "text": text, "elapsed": time.monotonic() - t0, "error": error,
    }


async def run_session(session_num: int, message_text: str) -> dict[str, Any]:
    try:
        session_id, transport = await open_session(session_num)
        return await call(session_num, session_id, transport, message_text)
    except Exception as exc:
        logger.error("Session-%02d failed: %s", session_num, exc, exc_info=True)
        return {
            "session_num": session_num, "session_id": None, "message": message_text,
            "text": "", "elapsed": 0.0, "error": {"message": str(exc)},
        }


# ── Test: NUM_GROUPS rounds x GROUP_SIZE concurrent sessions ────────────────

async def main() -> None:
    total = GROUP_SIZE * NUM_GROUPS
    print("=" * 70)
    print(f"client_multi_session.py (MCP) — provider={KEY_PROVIDER_NAME}")
    print("=" * 70)
    print(f"  BASE_URL = {BASE_URL}, tool = {TOOL_NAME}")
    print(f"  {GROUP_SIZE} x {NUM_GROUPS} = {total} sessions\n")

    all_results: list[dict] = []
    counter = 0

    for round_num in range(1, NUM_GROUPS + 1):
        group_msgs = MESSAGES[counter:counter + GROUP_SIZE]
        print(f"-- Round {round_num}/{NUM_GROUPS} (sessions {counter + 1}-{counter + GROUP_SIZE})")

        t_round = time.monotonic()
        group_results = await asyncio.gather(*[
            run_session(counter + i + 1, msg) for i, msg in enumerate(group_msgs)
        ])
        round_elapsed = time.monotonic() - t_round

        for r in group_results:
            n, err = r["session_num"], r["error"]
            if err:
                _fail(f"Session-{n:02d}", str(err.get("message", "?"))[:100])
            else:
                _ok(f"Session-{n:02d}  session_id={r['session_id']}  response={r['text'][:60]!r}  elapsed={r['elapsed']:.1f}s")

        print(f"  Round {round_num} completed in {round_elapsed:.1f}s")
        all_results.extend(group_results)
        counter += GROUP_SIZE

        if round_num < NUM_GROUPS:
            await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)

    errors = [r for r in all_results if r["error"]]
    session_ids = [r["session_id"] for r in all_results if r["session_id"]]
    unique_ids = set(session_ids)

    print(f"  Total sessions     : {len(all_results)}")
    print(f"  Errors             : {len(errors)}")
    print(f"  Unique session_ids : {len(unique_ids)}/{len(session_ids)}")

    ok = not errors and len(unique_ids) == len(session_ids)
    print("\n  RESULT: " + ("PASS" if ok else "ANOMALIES — see above"))


if __name__ == "__main__":
    asyncio.run(main())
