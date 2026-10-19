"""
_arqit_py_real_e2e_server_role.py
-----------------------------------
Helper NON eseguito direttamente da pytest — lanciato come processo
figlio separato da test_arqit_py_real_peering.py.

Avvia il solo ruolo SERVER (receiver) del provider arqit_py, poi resta
in ascolto su stdin per comandi JSON newline-delimited, inviati
dall'orchestratore (il test) per conto del ruolo CLIENT (eseguito in UN
ALTRO processo — vedi _arqit_py_real_e2e_client_role.py). Deliberatamente
nel suo processo OS: è proprio l'isolamento di processo a essere sotto
test (vedi ARQIT_PEERING_NOTES.md) — unire questo codice e quello del
ruolo CLIENT in un unico processo riproduce un 401 reale lato Arqit.

Protocollo (una riga JSON per comando, una riga JSON di risposta):
    {"cmd": "handshake", "client_id": str, "nonce": str}
        -> esegue perform_handshake() reale (stls_core.stls_core_handshake)
           usando la chiave ottenuta dal peering Arqit di questo processo
        <- {"cmd": "handshake_response", "session_id": str, "kid": str, "proof": str}
    {"cmd": "echo", "session_id": str, "envelope": {...}}
        -> decifra l'envelope (StlsTransport reale, AES-256-GCM + HMAC-SHA256)
           sulla sessione registrata dall'handshake, ricifra un payload di
           eco, dimostrando un round-trip di cifratura reale con la
           chiave negoziata via Arqit
        <- {"cmd": "echo_response", "envelope": {...}}
    {"cmd": "stop"}
        -> esce dal loop, stampa SERVER_DONE, termina

Uso: python _arqit_py_real_e2e_server_role.py [--debug]
Stampa "SERVER_READY" su stdout (e fa flush) non appena il provider è
avviato e in ascolto — il processo di test attende questa riga prima di
procedere.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time

logging.basicConfig(
    level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    stream=sys.stderr,
)

# Import SOLO dopo aver configurato il logging — stls_core non lo fa per
# noi (le applicazioni ospitanti, stls-a2a/stls-mcp, decidono la config).
import asyncio

from stls_core import HandshakeRequest, perform_handshake
from stls_core.stls_core_session import InMemorySessionCache
from stls_core.providers.stls_core_providers_factory import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider


def _wait_receiver_listening(timeout: float = 10.0) -> None:
    """
    Attende che il socket receiver dell'SDK Arqit sia effettivamente in
    listen() prima di segnalare SERVER_READY all'orchestratore.

    KeyProviderServerArqitPy.start() avvia il peering Arqit in un thread
    daemon e ritorna non appena il thread è stato lanciato — NON quando
    il socket è davvero bindato e in ascolto (bind()/listen() avvengono
    dentro server.listen_for_key_negotiation(), eseguito nel thread).
    Senza questa attesa, il ruolo CLIENT (processo separato) può tentare
    connect() prima che il listener sia pronto, ottenendo un
    ConnectionRefusedError reale — non un problema del cloud Arqit, ma
    una semplice race fra "thread avviato" e "socket in ascolto".
    """
    port = int(json.load(open(os.environ["ARQIT_PY_SERVER_CONFIG"])).get("SOCKET_LISTENING_PORT", 8889))
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return  # connect riuscito — il receiver è davvero in ascolto
            except OSError as exc:
                last_error = exc
        time.sleep(0.1)
    raise TimeoutError(
        f"receiver non in ascolto su 127.0.0.1:{port} dopo {timeout}s: {last_error}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.parse_args()

    # Deve avvenire a livello sincrono di modulo, non dentro un
    # asyncio.run(): KeySessionProviderFactory risolve lo start() async
    # del provider SERVER con un proprio asyncio.run() interno, che non
    # può essere annidato in un loop già in esecuzione.
    server_provider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        name="arqit_py",
    )
    # In-memory basta: questo processo vive solo per la durata di un
    # singolo test, nessun bisogno di condividerla con altri processi.
    session_cache = InMemorySessionCache()

    _wait_receiver_listening()
    print("SERVER_READY", flush=True)

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        msg = json.loads(raw_line)
        cmd = msg.get("cmd")

        if cmd == "stop":
            break

        if cmd == "handshake":
            request = HandshakeRequest(client_id=msg["client_id"], nonce=msg["nonce"])
            response = asyncio.run(perform_handshake(request, server_provider, session_cache))
            print(json.dumps({
                "cmd": "handshake_response",
                "session_id": response.session_id,
                "kid": response.kid,
                "proof": response.proof,
            }), flush=True)
            continue

        if cmd == "echo":
            transport = session_cache.get(msg["session_id"])
            if transport is None:
                print(json.dumps({"cmd": "echo_response", "error": "session not found"}), flush=True)
                continue
            payload = transport.decrypt_json(msg["envelope"])
            out_envelope = transport.encrypt_json({"echo": payload})
            print(json.dumps({"cmd": "echo_response", "envelope": out_envelope}), flush=True)
            continue

        print(json.dumps({"cmd": "error", "message": f"comando ignoto: {cmd!r}"}), flush=True)

    print("SERVER_DONE", flush=True)


if __name__ == "__main__":
    main()
