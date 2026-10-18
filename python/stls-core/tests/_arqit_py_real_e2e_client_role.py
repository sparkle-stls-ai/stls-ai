"""
_arqit_py_real_e2e_client_role.py
-----------------------------------
Helper NON eseguito direttamente da pytest — lanciato come processo
figlio separato da test_arqit_py_real_peering.py, dopo che il ruolo
SERVER (vedi _arqit_py_real_e2e_server_role.py) ha stampato SERVER_READY.

Avvia il solo ruolo CLIENT (initiator) del provider arqit_py, esegue il
key agreement reale con la cloud Arqit, poi — usando quella stessa
chiave — esegue un vero handshake STLS (proof-of-key-possession) e un
vero round-trip di cifratura (AES-256-GCM + HMAC-SHA256) con il
processo SERVER, tramite l'orchestratore (il test) che fa da relay tra
gli stdin/stdout dei due processi — questo script non parla mai
direttamente con l'altro, solo con l'orchestratore.

Sequenza di righe stampate su stdout (ognuna attesa dall'orchestratore
prima di proseguire):
    1. "KEY_OK kid=<kid> key_len=<n>"          — chiave Arqit ottenuta
    2. {"cmd": "handshake", "client_id": ..., "nonce": ...}
                                                 (poi legge da stdin la
                                                  risposta del server,
                                                  relayata dall'orchestratore)
    3. "HANDSHAKE_OK session_id=<sid>" oppure "CLIENT_FAILED <motivo>"
    4. {"cmd": "echo", "session_id": ..., "envelope": {...}}
                                                 (poi legge da stdin
                                                  l'eco cifrato, relayato
                                                  dall'orchestratore)
    5. "CLIENT_PASSED kid=<kid> key_len=<n> roundtrip=ok"
       oppure "CLIENT_FAILED <motivo>"

Exit code 0 = tutto il round-trip riuscito, 1 = eccezione o mismatch.
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
        # Stesso vincolo del ruolo SERVER: a livello sincrono di modulo,
        # non dentro un asyncio.run() già in esecuzione.
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

    # ── Handshake STLS reale (proof-of-key-possession) ──────────────────
    request = HandshakeRequest.generate(client_id="arqit-e2e-client")
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

    # ── Round-trip di cifratura reale (AES-256-GCM + HMAC-SHA256) ───────
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
