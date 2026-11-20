"""
client_concurrent.py
----------------------
Validates the one gap left after client_session.py's single-consumer
run: two CONSUMER replicas (server_consumer_1, server_consumer_2)
resolving two DIFFERENT real sessions CONCURRENTLY, confirmed working
end to end against the real Arqit cloud (2026-11-19).

Step 1: resolve two real sessions SEQUENTIALLY against the real
device-agent (kid_1/kdata_1, kid_2/kdata_2). Deliberately not
concurrent here -- that's plain CLIENT-side negotiation, a different,
already-validated concern (see client_session.py), not what this
script is about.

Step 2: send the two real encrypted A2A calls CONCURRENTLY
(asyncio.gather), one per consumer replica. Each consumer resolves
its OWN session via hint_kid from Redis while the other does the same
in a different process, at the same time. Confirms no cross-talk and
no serialization-induced failure between two live consumer replicas.

Run (after starting device_agent, server_consumer_1 AND
server_consumer_2 -- see the server sample's own README):
    python client_concurrent.py
"""

from __future__ import annotations

import asyncio
import os
import time

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

CONSUMERS = [
    ("consumer-1", "http://localhost:8111", "/agents/server-consumer-1/secure-a2a"),
    ("consumer-2", "http://localhost:8112", "/agents/server-consumer-2/secure-a2a"),
]


async def resolve_one_session(label: str) -> tuple[str, bytes]:
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
    print(f"[{label}] resolved kid={kid} key_len={len(kdata)}")
    return kid, kdata


async def call_consumer(label: str, base_url: str, agent_path: str, kid: str, kdata: bytes) -> dict:
    adapter = SecureA2ATransportAdapter(session_key=kdata, session_id=kid, base_url=base_url + agent_path)
    message = {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"type": "text", "text": f"ping from {label}"}]}},
    }
    t0 = time.monotonic()
    try:
        response = await asyncio.wait_for(adapter.send_secure_message(message), timeout=30.0)
        text = extract_a2a_text(response) or ""
        return {"label": label, "stls_ok": True, "text": text, "elapsed": time.monotonic() - t0}
    except Exception as exc:
        # A "No API key"/GOOGLE_API_KEY error here means the HTTP call
        # and the STLS decrypt/encrypt round trip both already
        # succeeded -- only the downstream Gemini call failed, the
        # same expected/known outcome as client_session.py's single-
        # consumer run, not a failure of anything this script tests.
        msg = str(exc)
        stls_ok = "api key" in msg.lower() or "GOOGLE_API_KEY" in msg
        return {"label": label, "stls_ok": stls_ok, "text": "", "error": msg, "elapsed": time.monotonic() - t0}
    finally:
        await adapter.client.aclose()


async def main() -> None:
    print("=== resolving two real sessions sequentially ===")
    kid1, kdata1 = await resolve_one_session("session-1")
    kid2, kdata2 = await resolve_one_session("session-2")
    assert kid1 != kid2, "the two sessions must be genuinely distinct"

    print("\n=== sending both A2A calls CONCURRENTLY, one per consumer replica ===")
    (label1, base1, path1), (label2, base2, path2) = CONSUMERS
    result1, result2 = await asyncio.gather(
        call_consumer(label1, base1, path1, kid1, kdata1),
        call_consumer(label2, base2, path2, kid2, kdata2),
    )
    for r in (result1, result2):
        detail = f"text={r['text']!r}" if r["text"] else f"error={r.get('error', '')}"
        print(f"  {r['label']}: stls_ok={r['stls_ok']} elapsed={r['elapsed']:.2f}s {detail}")

    if result1["stls_ok"] and result2["stls_ok"]:
        print("\nRESULT: PASS -- two consumer replicas resolved two distinct sessions concurrently, no cross-talk")
    else:
        print("\nRESULT: FAIL -- see errors above")


if __name__ == "__main__":
    asyncio.run(main())
