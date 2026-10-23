"""
client_multi_server.py — "1 client -> N servers" test with SecureSessionManager
---------------------------------------------------------------------------------
Minimal client (no ADK, no LLM): uses SecureSessionManager directly to
open encrypted sessions toward the two servers in
samples/server/10_server_ms/server_ms_1 and server_ms_2 from the same
process, and sends an A2A JSON-RPC message to each.

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py:
- base/pqc: no extra config — SecureSessionManager resolves the session
  for each base_url on its own (pqc does an HTTP handshake per
  base_url, base uses the shared fixed key).
- arqit_py: needs a DIFFERENT device identity per server, passed via
  arqit_client_configs={base_url: config_path} — exactly the feature
  just added to SecureSessionManager for this scenario (see
  stls_a2a_secure_remote.py).

Run (after starting server_ms_1 and server_ms_2):
    python client_multi_server.py
"""

import os

# Anchor the relative Arqit config paths (DEVICE_METADATA_FILEPATH,
# etc.) to this script's own directory — both aq-sf-client-ms-1.json
# and aq-sf-client-ms-2.json live here, with no filename collisions.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from stls_a2a import SecureSessionManager, extract_a2a_text

PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()

SERVERS = [
    ("server-ms-1", os.getenv("BASE_URL_1", "http://localhost:8101/agents/server-ms-1")),
    ("server-ms-2", os.getenv("BASE_URL_2", "http://localhost:8102/agents/server-ms-2")),
]

# Only relevant for arqit_py: a different device identity per server,
# resolved by base_url — the fallback would be ARQIT_PY_CLIENT_CONFIG
# (a single config for the whole process, intentionally not used here).
ARQIT_CLIENT_CONFIGS = {
    SERVERS[0][1]: os.getenv("ARQIT_PY_CLIENT_CONFIG_1", "./aq-sf-client-ms-1.json"),
    SERVERS[1][1]: os.getenv("ARQIT_PY_CLIENT_CONFIG_2", "./aq-sf-client-ms-2.json"),
}


def build_message(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            }
        },
    }


async def call_server(manager: SecureSessionManager, label: str, base_url: str) -> bool:
    """
    Returns True if the session + round trip succeeded.
    A failure on one server (e.g. missing GOOGLE_API_KEY on the ADK
    side) must not prevent trying the others — this is validating the
    session/transport layer, not the LLM's answer.
    """
    logger.info("[%s] requesting %s session toward %s", label, PROVIDER_NAME, base_url)
    try:
        adapter = await manager.get_adapter(base_url)
        message = build_message(f"ping from client (provider={PROVIDER_NAME})")
        response = await adapter.send_secure_message(message)
        text = extract_a2a_text(response)
        print(f"  [{label}] OK — response: {text}")
        return True
    except Exception as exc:
        print(f"  [{label}] FAILED — {exc}")
        return False


async def main() -> None:
    manager = SecureSessionManager(
        client_id="client-10-ms",
        provider_name=PROVIDER_NAME,
        arqit_client_configs=ARQIT_CLIENT_CONFIGS if PROVIDER_NAME == "arqit_py" else None,
    )

    print(f"\n=== Multi-server test — provider={PROVIDER_NAME} ===")
    results: dict[str, bool] = {}
    try:
        for label, base_url in SERVERS:
            results[label] = await call_server(manager, label, base_url)
    finally:
        await manager.aclose()

    n_ok = sum(1 for ok in results.values() if ok)
    print(f"=== Done — {n_ok}/{len(results)} servers OK ===\n")


if __name__ == "__main__":
    asyncio.run(main())
