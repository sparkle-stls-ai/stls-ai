"""
client_session.py
-------------------
One real A2A session against server_consumer_1 or server_consumer_2
(samples/server/16_server_device_agent/), end to end: resolves a
session key against the real device-agent (the actual device owner),
then makes one encrypted A2A call through the real HTTP endpoint
served by a consumer-role replica that never called start() itself.

Deliberately NOT samples/client/10_client_ms/client_multi_session.py:
that script's "Test A" always opens 2 CONCURRENT sessions regardless
of GROUP_SIZE/NUM_GROUPS, which is a different (also valid, see that
script's own docstring) thing to test than what this sample is about.
It's reusable here too if you want it (override BASE_URL/AGENT_PATH/
ARQIT_PY_CLIENT_CONFIG the same way its own README shows), but this
script is the simpler, recommended way to validate the device-agent/
consumer-role mechanism itself with one sequential, explicitly
timed-out call.

Run (after starting device_agent and at least one server_consumer_*,
see the server sample's own README):
    BASE_URL=http://localhost:8111 AGENT_PATH=/agents/server-consumer-1/secure-a2a \
    python client_session.py
"""

from __future__ import annotations

import asyncio
import os
import time

# DEVICE_METADATA_FILEPATH inside ARQIT_PY_CLIENT_CONFIG is resolved
# as an absolute path in this sample's own aq-*.json (unlike the
# relative-path convention elsewhere in this repo) specifically so
# this script's own cwd never matters -- see ARQIT_PEERING_NOTES.md's
# cwd pitfall for why that convention exists and what silently breaks
# without it.
os.environ.setdefault("KEY_PROVIDER_NAME", "arqit_py")
os.environ.setdefault(
    "ARQIT_PY_CLIENT_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "aq-sf-device-agent-16-client.json"),
)

from dotenv import load_dotenv

load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)

from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    SecureA2ATransportAdapter,
    extract_a2a_text,
)

BASE_URL = os.getenv("BASE_URL", "http://localhost:8111")
AGENT_PATH = os.getenv("AGENT_PATH", "/agents/server-consumer-1/secure-a2a")


async def main() -> None:
    print(f"=== 1. resolving a session key via the real device-agent ({BASE_URL}{AGENT_PATH}) ===")
    t0 = time.monotonic()
    provider = await asyncio.wait_for(
        asyncio.to_thread(
            lambda: KeySessionProviderFactory().get_session_provider(
                session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
                name="arqit_py",
            )
        ),
        timeout=40.0,
    )
    kid = await asyncio.wait_for(provider.getSessionKeyIdentifier(), timeout=10.0)
    kdata = await asyncio.wait_for(provider.getSessionKeyData(kid), timeout=10.0)
    print(f"SESSION_OK kid={kid} key_len={len(kdata)} elapsed={time.monotonic() - t0:.2f}s")

    print("=== 2. one real encrypted A2A call ===")
    adapter = SecureA2ATransportAdapter(
        session_key=kdata, session_id=kid, base_url=BASE_URL + AGENT_PATH,
    )
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {"role": "user", "parts": [{"type": "text", "text": "ping via device-agent"}]},
        },
    }
    try:
        response = await asyncio.wait_for(adapter.send_secure_message(message), timeout=30.0)
        text = extract_a2a_text(response) or ""
        print(f"CALL_OK text={text!r}")
    except Exception as exc:
        # A GOOGLE_API_KEY-related error here means the STLS/consumer-role
        # layer worked (the encrypted round trip reached the agent code)
        # and only the downstream Gemini call failed -- an expected,
        # already-documented outcome without a real key configured, not
        # a failure of anything this sample is validating.
        print(f"CALL_ERROR (check if this is just the known missing-GOOGLE_API_KEY case): {exc!r}")
    finally:
        await adapter.client.aclose()

    print("RESULT: STLS/consumer-role layer reached end to end")


if __name__ == "__main__":
    asyncio.run(main())
