"""
test_stls_core_handshake.py
----------------------------
Unit test per perform_handshake() e verify_handshake_proof().

Copertura:
  H01 — handshake con provider Base: session_id, kid, proof presenti
  H02 — proof verifica correttamente la chiave (verify_handshake_proof)
  H03 — proof con chiave sbagliata → False
  H04 — session_id generato dal server è un UUID valido
  H05 — sessione registrata in SessionCache dopo handshake
  H06 — HandshakeRequest.generate() produce nonce base64 valido ≥ 16 byte
  H07 — nonce troppo corto → HandshakeError code -32600
  H08 — nonce non base64 → HandshakeError code -32600
  H09 — provider che restituisce None → HandshakeError
  H10 — due handshake producono session_id diversi
  H11 — transport in cache decifra correttamente dopo handshake
"""

import asyncio
import base64
import os
import uuid

import pytest

from stls_core.stls_core_handshake import (
    HandshakeError,
    HandshakeRequest,
    HandshakeResponse,
    perform_handshake,
    verify_handshake_proof,
)
from stls_core.stls_core_session import SessionCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cache() -> SessionCache:
    return SessionCache(max_size=16, ttl_seconds=60)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# H01 — handshake base: campi risposta presenti
# ---------------------------------------------------------------------------

def test_H01_handshake_base_fields(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    resp = run(perform_handshake(req, base_provider_session_server, cache))

    assert resp.session_id
    assert resp.kid
    assert resp.proof
    assert resp.encryption == "AES-256-GCM"
    assert resp.hmac == "HMAC-SHA256"
    assert resp.key_derivation == "HKDF-Expand-SHA256"


# ---------------------------------------------------------------------------
# H02 — proof corretto
# ---------------------------------------------------------------------------

def test_H02_proof_valid(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    resp = run(perform_handshake(req, base_provider_session_server, cache))

    # Il client usa la stessa chiave del provider base
    from stls_core.providers.stls_core_providers_base import _BASE_KEY_DATA
    session_key = _BASE_KEY_DATA

    valid = verify_handshake_proof(
        session_key=session_key,
        nonce=req.nonce,
        response=resp,
    )
    assert valid is True


# ---------------------------------------------------------------------------
# H03 — proof con chiave sbagliata → False
# ---------------------------------------------------------------------------

def test_H03_proof_wrong_key(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    resp = run(perform_handshake(req, base_provider_session_server, cache))

    wrong_key = os.urandom(32)
    valid = verify_handshake_proof(
        session_key=wrong_key,
        nonce=req.nonce,
        response=resp,
    )
    assert valid is False


# ---------------------------------------------------------------------------
# H04 — session_id è UUID valido
# ---------------------------------------------------------------------------

def test_H04_session_id_is_uuid(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    resp = run(perform_handshake(req, base_provider_session_server, cache))

    parsed = uuid.UUID(resp.session_id)  # solleva ValueError se non è UUID
    assert str(parsed) == resp.session_id


# ---------------------------------------------------------------------------
# H05 — sessione registrata in cache
# ---------------------------------------------------------------------------

def test_H05_session_in_cache(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    resp = run(perform_handshake(req, base_provider_session_server, cache))

    transport = cache.get(resp.session_id)
    assert transport is not None
    assert transport.session_id == resp.session_id


# ---------------------------------------------------------------------------
# H06 — HandshakeRequest.generate produce nonce valido
# ---------------------------------------------------------------------------

def test_H06_generate_nonce():
    req = HandshakeRequest.generate(client_id="client-1")
    nonce_bytes = base64.b64decode(req.nonce)
    assert len(nonce_bytes) == 32


# ---------------------------------------------------------------------------
# H07 — nonce troppo corto → HandshakeError -32600
# ---------------------------------------------------------------------------

def test_H07_nonce_too_short(base_provider_session_server):
    cache = make_cache()
    short_nonce = base64.b64encode(b"short").decode()
    req = HandshakeRequest(client_id="test-client", nonce=short_nonce)

    with pytest.raises(HandshakeError) as exc_info:
        run(perform_handshake(req, base_provider_session_server, cache))

    assert exc_info.value.code == -32600
    assert "too short" in exc_info.value.message


# ---------------------------------------------------------------------------
# H08 — nonce non base64 → HandshakeError -32600
# ---------------------------------------------------------------------------

def test_H08_nonce_invalid_base64(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest(client_id="test-client", nonce="!!!non-base64!!!")

    with pytest.raises(HandshakeError) as exc_info:
        run(perform_handshake(req, base_provider_session_server, cache))

    assert exc_info.value.code == -32600


# ---------------------------------------------------------------------------
# H09 — provider che restituisce None → HandshakeError
# ---------------------------------------------------------------------------

class _NullProvider:
    async def getSessionKeyIdentifier(self):
        return "kid-null"

    async def getSessionKeyData(self, kid):
        return None  # simula provider non disponibile


def test_H09_provider_returns_none():
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    provider = _NullProvider()

    with pytest.raises(HandshakeError):
        run(perform_handshake(req, provider, cache))


# ---------------------------------------------------------------------------
# H10 — due handshake producono session_id diversi
# ---------------------------------------------------------------------------

def test_H10_unique_session_ids(base_provider_session_server):
    cache = make_cache()
    req1 = HandshakeRequest.generate(client_id="client-A")
    req2 = HandshakeRequest.generate(client_id="client-B")

    resp1 = run(perform_handshake(req1, base_provider_session_server, cache))
    resp2 = run(perform_handshake(req2, base_provider_session_server, cache))

    assert resp1.session_id != resp2.session_id


# ---------------------------------------------------------------------------
# H11 — transport in cache decifra correttamente
# ---------------------------------------------------------------------------

def test_H11_transport_encrypt_decrypt(base_provider_session_server):
    cache = make_cache()
    req = HandshakeRequest.generate(client_id="test-client")
    resp = run(perform_handshake(req, base_provider_session_server, cache))

    transport = cache.get(resp.session_id)
    assert transport is not None

    plaintext = b"payload di test post-handshake"
    envelope = transport.encrypt(plaintext)
    recovered = transport.decrypt(envelope)
    assert recovered == plaintext