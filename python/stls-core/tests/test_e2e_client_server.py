"""
test_e2e_client_server.py
---------------------------
Test end-to-end "over-the-wire": un vero server FastAPI (uvicorn, in un
thread separato) e un vero client httpx che parlano HTTP reale.

Valida l'intero flusso protocol-agnostic di stls-core su un canale di
rete reale (localhost):

    1. Client genera HandshakeRequest (nonce random)
    2. POST /stls/handshake  →  server esegue perform_handshake()
    3. Client verifica il proof (verify_handshake_proof) — niente key in transito
    4. Client cifra un payload JSON con StlsTransport
    5. POST /stls/echo  →  server decifra, processa, ricifra la risposta
    6. Client decifra la risposta e verifica il contenuto

Questo NON sostituisce i test unitari (test_stls_core_*.py): qui si
verifica l'integrazione end-to-end via HTTP reale, inclusa la
serializzazione JSON degli envelope su httpx/FastAPI — cosa che i test
in-process non possono garantire.

Dipendenze: fastapi, uvicorn, httpx (extra [dev], non runtime).

Server di test:
    POST /stls/handshake   → HandshakeResponse
    POST /stls/echo        → riceve envelope cifrato, decifra, fa eco
                              del payload con un campo "echo" aggiunto,
                              ricifra e restituisce
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import closing

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from stls_core.stls_core_constants import HEADER_SESSION_ID
from stls_core.stls_core_handshake import (
    HandshakeRequest,
    HandshakeResponse,
    perform_handshake,
    verify_handshake_proof,
)
from stls_core.stls_core_session import SessionCache
from stls_core.providers.stls_core_providers_base import (
    KeySessionProvider,
    KeySessionProviderBase,
    _BASE_KEY_DATA,
)


# ---------------------------------------------------------------------------
# Server FastAPI di test — endpoint minimi handshake + echo cifrato
# ---------------------------------------------------------------------------

def _build_test_app() -> FastAPI:
    """
    Costruisce l'app FastAPI di test con i due endpoint STLS minimi.

    La SessionCache e il provider sono creati per-app (non globali) per
    garantire isolamento tra esecuzioni di test diverse.
    """
    app = FastAPI()
    app.state.session_cache = SessionCache(max_size=16, ttl_seconds=30)
    app.state.provider = KeySessionProviderBase(
        KeySessionProvider.KeySessionProviderType.SERVER
    )

    @app.post("/stls/handshake")
    async def handshake_endpoint(request: HandshakeRequest) -> HandshakeResponse:
        try:
            return await perform_handshake(
                request, app.state.provider, app.state.session_cache
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/stls/echo")
    async def echo_endpoint(request: Request) -> dict:
        """
        Riceve un envelope cifrato, lo decifra, aggiunge un campo "echo"
        al payload, ricifra e restituisce lo stesso envelope (stessa
        session_id, nuovo IV/ciphertext/tag/mac).
        """
        session_id = request.headers.get(HEADER_SESSION_ID)
        if not session_id:
            raise HTTPException(status_code=400, detail=f"Header {HEADER_SESSION_ID} mancante")

        transport = app.state.session_cache.get(session_id)
        if transport is None:
            raise HTTPException(status_code=401, detail="Sessione non valida o scaduta")

        envelope_dict = await request.json()
        try:
            payload = transport.decrypt_json(envelope_dict)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Decrypt fallito: {exc}") from exc

        # Logica applicativa: eco con conferma server-side
        response_payload = {**payload, "echo": True, "server_processed": True}

        return transport.encrypt_json(response_payload)

    return app


# ---------------------------------------------------------------------------
# Server runner — uvicorn in thread daemon
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    """Trova una porta TCP libera sull'host locale."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornTestServer:
    """
    Avvia un vero server uvicorn in un thread daemon, con shutdown pulito.

    Uso:
        with _UvicornTestServer(app) as base_url:
            httpx.post(f"{base_url}/stls/handshake", ...)
    """

    def __init__(self, app: FastAPI) -> None:
        self.port = _find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port,
            log_level="warning", loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    def __enter__(self) -> str:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        # Attendi che il server sia pronto (polling su started flag)
        deadline = time.monotonic() + 10.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("Server uvicorn non avviato entro 10s")
            time.sleep(0.05)

        return self.base_url

    def __exit__(self, *exc_info) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Fixture pytest
# ---------------------------------------------------------------------------

@pytest.fixture
def e2e_server():
    """Avvia il server di test per la durata del test, restituisce base_url."""
    app = _build_test_app()
    with _UvicornTestServer(app) as base_url:
        yield base_url


@pytest.fixture
def http_client(e2e_server):
    """Client httpx sincrono puntato al server di test."""
    with httpx.Client(base_url=e2e_server, timeout=10.0) as client:
        yield client


# ---------------------------------------------------------------------------
# Helper: esegue l'handshake completo lato client
# ---------------------------------------------------------------------------

def _do_handshake(client: httpx.Client, client_id: str = "e2e-test-client"):
    """
    Esegue l'handshake HTTP reale e verifica il proof.

    Returns:
        (HandshakeResponse, session_key) — session_key è quella nota
        lato client per il provider 'base' (cablata, condivisa).
    """
    req = HandshakeRequest.generate(client_id=client_id)

    http_resp = client.post("/stls/handshake", json=req.model_dump())
    assert http_resp.status_code == 200, http_resp.text

    resp = HandshakeResponse.model_validate(http_resp.json())

    # Il client 'base' conosce la stessa chiave cablata del server
    session_key = _BASE_KEY_DATA

    valid = verify_handshake_proof(
        session_key=session_key, nonce=req.nonce, response=resp
    )
    assert valid, "Proof of possession non valido — chiavi client/server diverse"

    return resp, session_key


# ---------------------------------------------------------------------------
# E01 — handshake HTTP reale completa con successo
# ---------------------------------------------------------------------------

def test_E01_handshake_over_http(http_client):
    resp, session_key = _do_handshake(http_client)
    assert resp.session_id
    assert resp.kid
    assert resp.proof


# ---------------------------------------------------------------------------
# E02 — round-trip cifrato completo: encrypt client → decrypt server →
#        encrypt server → decrypt client
# ---------------------------------------------------------------------------

def test_E02_encrypted_roundtrip_over_http(http_client):
    from stls_core.stls_core_crypto import StlsTransport

    resp, session_key = _do_handshake(http_client)

    client_transport = StlsTransport(
        session_key=session_key, session_id=resp.session_id
    )

    payload = {"method": "tools/call", "params": {"name": "get_weather", "city": "Palermo"}}
    envelope = client_transport.encrypt_json(payload)

    http_resp = http_client.post(
        "/stls/echo",
        json=envelope,
        headers={HEADER_SESSION_ID: resp.session_id},
    )
    assert http_resp.status_code == 200, http_resp.text

    response_envelope = http_resp.json()
    decrypted = client_transport.decrypt_json(response_envelope)

    assert decrypted["method"] == "tools/call"
    assert decrypted["params"]["city"] == "Palermo"
    assert decrypted["echo"] is True
    assert decrypted["server_processed"] is True


# ---------------------------------------------------------------------------
# E03 — richiesta senza header session_id → 400
# ---------------------------------------------------------------------------

def test_E03_missing_session_header(http_client):
    resp, session_key = _do_handshake(http_client)

    from stls_core.stls_core_crypto import StlsTransport
    transport = StlsTransport(session_key=session_key, session_id=resp.session_id)
    envelope = transport.encrypt_json({"foo": "bar"})

    http_resp = http_client.post("/stls/echo", json=envelope)  # niente header
    assert http_resp.status_code == 400


# ---------------------------------------------------------------------------
# E04 — session_id sconosciuto → 401
# ---------------------------------------------------------------------------

def test_E04_unknown_session_id(http_client):
    from stls_core.stls_core_crypto import StlsTransport

    fake_session_id = "sessione-mai-esistita-0000"
    transport = StlsTransport(session_key=_BASE_KEY_DATA, session_id=fake_session_id)
    envelope = transport.encrypt_json({"foo": "bar"})

    http_resp = http_client.post(
        "/stls/echo",
        json=envelope,
        headers={HEADER_SESSION_ID: fake_session_id},
    )
    assert http_resp.status_code == 401


# ---------------------------------------------------------------------------
# E05 — payload manomesso in transito → 400 (MAC/GCM fallisce)
# ---------------------------------------------------------------------------

def test_E05_tampered_payload_rejected(http_client):
    from stls_core.stls_core_crypto import StlsTransport

    resp, session_key = _do_handshake(http_client)
    transport = StlsTransport(session_key=session_key, session_id=resp.session_id)
    envelope = transport.encrypt_json({"sensitive": "data"})

    # Manomette il ciphertext (simula un attaccante MITM)
    import base64
    ct = bytearray(base64.b64decode(envelope["ciphertext"]))
    ct[0] ^= 0xFF
    envelope["ciphertext"] = base64.b64encode(bytes(ct)).decode()

    http_resp = http_client.post(
        "/stls/echo",
        json=envelope,
        headers={HEADER_SESSION_ID: resp.session_id},
    )
    assert http_resp.status_code == 400


# ---------------------------------------------------------------------------
# E06 — due handshake sullo stesso server producono sessioni indipendenti
# ---------------------------------------------------------------------------

def test_E06_two_independent_sessions(http_client):
    from stls_core.stls_core_crypto import StlsTransport

    resp_a, key_a = _do_handshake(http_client, client_id="client-A")
    resp_b, key_b = _do_handshake(http_client, client_id="client-B")

    assert resp_a.session_id != resp_b.session_id

    transport_a = StlsTransport(session_key=key_a, session_id=resp_a.session_id)
    transport_b = StlsTransport(session_key=key_b, session_id=resp_b.session_id)

    envelope_a = transport_a.encrypt_json({"client": "A"})
    envelope_b = transport_b.encrypt_json({"client": "B"})

    http_resp_a = http_client.post(
        "/stls/echo", json=envelope_a,
        headers={HEADER_SESSION_ID: resp_a.session_id},
    )
    http_resp_b = http_client.post(
        "/stls/echo", json=envelope_b,
        headers={HEADER_SESSION_ID: resp_b.session_id},
    )

    assert http_resp_a.status_code == 200
    assert http_resp_b.status_code == 200

    decrypted_a = transport_a.decrypt_json(http_resp_a.json())
    decrypted_b = transport_b.decrypt_json(http_resp_b.json())

    assert decrypted_a["client"] == "A"
    assert decrypted_b["client"] == "B"


# ---------------------------------------------------------------------------
# E07 — wire format: il body HTTP è davvero JSON, niente bytes grezzi
# ---------------------------------------------------------------------------

def test_E07_wire_format_is_json(http_client):
    resp, session_key = _do_handshake(http_client)

    from stls_core.stls_core_crypto import StlsTransport
    transport = StlsTransport(session_key=session_key, session_id=resp.session_id)
    envelope = transport.encrypt_json({"k": "v"})

    # Verifica che tutti i campi siano stringhe JSON-safe (no bytes)
    for key, value in envelope.items():
        assert isinstance(value, str), f"Campo {key} non è una stringa JSON-safe"