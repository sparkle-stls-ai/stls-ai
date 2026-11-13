"""
_arqit_py_real_e2e_client_role_eviction.py
--------------------------------------------
Not run directly by pytest — launched as a separate child process by
test_arqit_py_real_peering_eviction.py, after the SERVER role (see
_arqit_py_real_e2e_server_role.py) has printed SERVER_READY.

Same CLIENT (initiator) role as _arqit_py_real_e2e_client_role.py: real
key agreement against the Arqit cloud, a real STLS handshake
(proof-of-key-possession), and a real AES-256-GCM+HMAC-SHA256
encrypt/decrypt round trip with the SERVER process.

What this script adds (issue #34): after the round trip above succeeds
— proving this kid is a real, cloud-delivered key, not a fixture — it
chains the two pieces that were previously only verified independently:

  1. calls `client_provider.releaseSessionKeyData(kid)` — the exact
     same call the Arqit sidecar's `_evict_key()` makes from its "stop"
     RPC / connection-close path (see sidecar/arqit_sidecar.py).
  2. immediately tries `client_provider.getSessionKeyData(kid)` again
     on the SAME provider instance (same key store) and expects a
     KeyError — the key store no longer has this kid.

If eviction didn't actually remove the key, step 2 would succeed
instead of raising — that's the one thing neither the teardown-call
check nor the key-store unit tests (both already covered elsewhere)
individually proved.

Stdout protocol (same as _arqit_py_real_e2e_client_role.py), with one
extra line appended after "CLIENT_PASSED ...":
    "EVICTION_OK kid=<kid>"
        — release() succeeded and the key is confirmed gone
    "EVICTION_FAILED <reason>"
        — either release() itself raised, the re-fetch raised something
          other than KeyError, or (worst case) the re-fetch *succeeded*,
          meaning the key was never actually evicted

Exit code 0 only if the full round trip AND the eviction probe both
passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback

logging.basicConfig(
    level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    stream=sys.stderr,
)

from stls_core import HandshakeRequest, HandshakeResponse, verify_handshake_proof
from stls_core.stls_core_crypto import StlsTransport
from stls_core.providers.stls_core_providers_factory import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider

_TEST_PAYLOAD = {"ping": "secured-over-real-arqit-key", "n": 42}


async def _fetch_key(provider) -> tuple[str, bytes]:
    kid = await provider.getSessionKeyIdentifier()
    key = await provider.getSessionKeyData(kid)
    return kid, bytes(key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.parse_args()

    try:
        # Same constraint as the SERVER role: must happen at module-level
        # sync code, not inside an already-running asyncio.run().
        client_provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            name="arqit_py",
        )
        kid, key = asyncio.run(_fetch_key(client_provider))
    except Exception:
        traceback.print_exc()
        print("CLIENT_FAILED key_agreement_error", flush=True)
        return 1

    print(f"KEY_OK kid={kid} key_len={len(key)}", flush=True)

    # ── Real STLS handshake (proof-of-key-possession) ───────────────────
    request = HandshakeRequest.generate(client_id="arqit-e2e-client-eviction")
    print(json.dumps({
        "cmd": "handshake", "client_id": request.client_id, "nonce": request.nonce,
    }), flush=True)

    resp_line = sys.stdin.readline()
    try:
        resp_msg = json.loads(resp_line)
        response = HandshakeResponse(
            session_id=resp_msg["session_id"],
            kid=resp_msg["kid"],
            proof=resp_msg["proof"],
        )
    except Exception:
        traceback.print_exc()
        print(f"CLIENT_FAILED bad_handshake_response {resp_line!r}", flush=True)
        return 1

    if not verify_handshake_proof(key, request.nonce, response):
        print("CLIENT_FAILED proof_mismatch", flush=True)
        return 1

    print(f"HANDSHAKE_OK session_id={response.session_id} kid={response.kid}", flush=True)

    # ── Real encrypt/decrypt round trip (AES-256-GCM + HMAC-SHA256) ─────
    transport = StlsTransport(session_key=key, session_id=response.session_id)
    out_envelope = transport.encrypt_json(_TEST_PAYLOAD)
    print(json.dumps({
        "cmd": "echo", "session_id": response.session_id, "envelope": out_envelope,
    }), flush=True)

    echo_line = sys.stdin.readline()
    try:
        echo_msg = json.loads(echo_line)
        decrypted = transport.decrypt_json(echo_msg["envelope"])
    except Exception:
        traceback.print_exc()
        print(f"CLIENT_FAILED bad_echo_response {echo_line!r}", flush=True)
        return 1

    if decrypted.get("echo") != _TEST_PAYLOAD:
        print(f"CLIENT_FAILED echo_mismatch got={decrypted!r}", flush=True)
        return 1

    print(f"CLIENT_PASSED kid={kid} key_len={len(key)} roundtrip=ok", flush=True)

    # ── Eviction probe (issue #34) ───────────────────────────────────────
    # Chains the two pieces that were, until now, only verified in
    # isolation: the teardown-call path (releaseSessionKeyData) and the
    # key store's remove()/get() behavior — against this real,
    # cloud-delivered kid, on the very provider instance that resolved it.
    try:
        client_provider.releaseSessionKeyData(kid)
    except Exception:
        traceback.print_exc()
        print("EVICTION_FAILED release_raised", flush=True)
        return 1

    async def _refetch() -> bytes:
        return await client_provider.getSessionKeyData(kid)

    try:
        asyncio.run(_refetch())
    except KeyError:
        print(f"EVICTION_OK kid={kid}", flush=True)
        return 0
    except Exception:
        traceback.print_exc()
        print("EVICTION_FAILED unexpected_error_on_refetch", flush=True)
        return 1
    else:
        # Worst case: no exception at all — the key is still there.
        print(f"EVICTION_FAILED key_still_present kid={kid}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
