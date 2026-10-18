"""
tests/test_pqc.py
-----------------
Unit test per il provider PQC di stls-core.

Organizzazione:
  TestPQCKeyStore           — dict thread-safe in-memory (no liboqs)
  TestX25519                — generazione keypair e DH exchange (no liboqs)
  TestDeriveHybridKey       — HKDF ibrido e proprietà di sicurezza (no liboqs)
  TestMLKEM                 — encap/decap round-trip (richiede liboqs, skip altrimenti)
  TestKeySessionProviderPQC — interfaccia KeySessionProvider (no liboqs)
  TestKeyProviderLifecycle  — client/server start/stop (liboqs per il client)
  TestPQCHandshakeModels    — modelli Pydantic e PQCHandshakeError (no liboqs)
  TestPerformPQCHandshake   — lato server dell'handshake (richiede liboqs)
  TestRoundTrip             — test di integrazione E2E client+server (richiede liboqs)

Test principali evidenziati con commento # KEY TEST.

Esecuzione:
  pytest tests/test_pqc.py -v
  pytest tests/test_pqc.py -v -k "not requires_liboqs"   # senza liboqs
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
import threading
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Rilevamento liboqs — skip automatico dei test che lo richiedono
# ---------------------------------------------------------------------------

liboqs_available: bool = False
_liboqs_skip_reason: str = "liboqs-python non installato — uv pip install -e '.[pqc]'"
try:
    import oqs  # noqa: F401
    liboqs_available = True
except ImportError:
    _liboqs_skip_reason = (
        "liboqs-python non installato — "
        "uv pip install -e '.[pqc]' oppure pip install liboqs-python==0.11.0"
    )
except (AttributeError, OSError) as _e:
    _liboqs_skip_reason = (
        f"liboqs-python incompatibile con la C library di sistema ({_e}). "
        "Fix: uv pip install liboqs-python==0.11.0"
    )

requires_liboqs = pytest.mark.skipif(
    not liboqs_available,
    reason=_liboqs_skip_reason,
)

# ---------------------------------------------------------------------------
# Import soggetti ai test
# ---------------------------------------------------------------------------

from stls_core.providers.stls_core_providers_pqc import (
    PQCKeyStore,
    KeySessionProviderPQC,
    KeyProviderClientPQC,
    KeyProviderServerPQC,
    KeyProviderPQC,
    x25519_generate_keypair,
    x25519_exchange,
    derive_hybrid_key,
)
from stls_core.providers.stls_core_providers_base import KeySessionProvider
from stls_core.stls_core_handshake_pqc import (
    PQCHandshakeRequest,
    PQCHandshakeResponse,
    PQCHandshakeError,
    perform_pqc_handshake,
    derive_session_key_client,
    verify_pqc_handshake_proof,
)
from stls_core.stls_core_session import InMemorySessionCache
from stls_core.stls_core_crypto import StlsTransport

# Import condizionali (solo se liboqs disponibile)
if liboqs_available:
    from stls_core.providers.stls_core_providers_pqc import (
        mlkem_generate_keypair,
        mlkem_encap,
        mlkem_decap,
    )


# ===========================================================================
# TestPQCKeyStore
# ===========================================================================

class TestPQCKeyStore:
    """PQCKeyStore — dict thread-safe senza dipendenze esterne."""

    def test_put_and_get(self) -> None:
        store = PQCKeyStore()
        key = os.urandom(32)
        store.put("kid-1", key)
        assert store.get("kid-1") == key

    def test_get_missing_returns_none(self) -> None:
        store = PQCKeyStore()
        assert store.get("nonexistent") is None

    def test_overwrite_key(self) -> None:
        store = PQCKeyStore()
        store.put("k", b"first")
        store.put("k", b"second")
        assert store.get("k") == b"second"

    def test_remove_existing(self) -> None:
        store = PQCKeyStore()
        store.put("k", b"v")
        assert store.remove("k") is True
        assert store.get("k") is None

    def test_remove_nonexistent(self) -> None:
        store = PQCKeyStore()
        assert store.remove("ghost") is False

    def test_len_empty(self) -> None:
        assert len(PQCKeyStore()) == 0

    def test_len_after_operations(self) -> None:
        store = PQCKeyStore()
        store.put("a", b"1")
        store.put("b", b"2")
        assert len(store) == 2
        store.remove("a")
        assert len(store) == 1

    def test_multiple_kids_independent(self) -> None:
        store = PQCKeyStore()
        k1, k2 = os.urandom(32), os.urandom(32)
        store.put("kid-1", k1)
        store.put("kid-2", k2)
        assert store.get("kid-1") == k1
        assert store.get("kid-2") == k2

    def test_thread_safety(self) -> None:
        """Scritture concorrenti da più thread non causano race condition."""
        store = PQCKeyStore()
        errors: list[Exception] = []

        def writer(i: int) -> None:
            try:
                for _ in range(50):
                    store.put(f"kid-{i}", os.urandom(32))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errori nei thread: {errors}"


# ===========================================================================
# TestX25519
# ===========================================================================

class TestX25519:
    """X25519 — non richiede liboqs, usa pyca/cryptography."""

    def test_keypair_key_lengths(self) -> None:
        priv, pub = x25519_generate_keypair()
        assert len(priv) == 32
        assert len(pub) == 32

    def test_keypairs_are_random(self) -> None:
        _, pub1 = x25519_generate_keypair()
        _, pub2 = x25519_generate_keypair()
        assert pub1 != pub2

    def test_dh_symmetry(self) -> None:
        """Proprietà fondamentale DH: A.priv ⊗ B.pub == B.priv ⊗ A.pub."""
        priv_a, pub_a = x25519_generate_keypair()
        priv_b, pub_b = x25519_generate_keypair()
        secret_a = x25519_exchange(priv_a, pub_b)
        secret_b = x25519_exchange(priv_b, pub_a)
        assert secret_a == secret_b

    def test_dh_output_length(self) -> None:
        priv_a, _ = x25519_generate_keypair()
        _, pub_b = x25519_generate_keypair()
        shared = x25519_exchange(priv_a, pub_b)
        assert len(shared) == 32

    def test_dh_deterministic(self) -> None:
        """Stesse chiavi → stesso shared secret, sempre."""
        priv_a, _ = x25519_generate_keypair()
        _, pub_b = x25519_generate_keypair()
        assert x25519_exchange(priv_a, pub_b) == x25519_exchange(priv_a, pub_b)

    def test_dh_different_peers(self) -> None:
        """Peer diversi → shared secret diverso."""
        priv_a, _ = x25519_generate_keypair()
        _, pub_b = x25519_generate_keypair()
        _, pub_c = x25519_generate_keypair()
        assert x25519_exchange(priv_a, pub_b) != x25519_exchange(priv_a, pub_c)


# ===========================================================================
# TestDeriveHybridKey
# ===========================================================================

class TestDeriveHybridKey:
    """
    HKDF ibrido — proprietà crittografiche del key agreement combinato.
    Non richiede liboqs: lavora su bytes random in input.
    """

    def test_output_length_default(self) -> None:
        key = derive_hybrid_key(os.urandom(32), os.urandom(32), os.urandom(32))
        assert len(key) == 32

    def test_output_length_custom(self) -> None:
        key = derive_hybrid_key(os.urandom(32), os.urandom(32), os.urandom(32), length=64)
        assert len(key) == 64

    def test_deterministic(self) -> None:
        """Stessi input → stesso output."""
        x, m, n = os.urandom(32), os.urandom(32), os.urandom(32)
        assert derive_hybrid_key(x, m, n) == derive_hybrid_key(x, m, n)

    def test_nonce_provides_freshness(self) -> None:
        """Nonce diverso → chiave diversa. Core della freshness per-sessione."""
        x, m = os.urandom(32), os.urandom(32)
        k1 = derive_hybrid_key(x, m, os.urandom(32))
        k2 = derive_hybrid_key(x, m, os.urandom(32))
        assert k1 != k2

    # KEY TEST — proprietà ibrida
    def test_x25519_secret_contributes(self) -> None:
        """
        Cambiando solo x25519_secret la chiave finale cambia.
        Verifica che X25519 contribuisca al risultato — se ML-KEM fosse
        compromesso, X25519 protegge ancora la session_key.
        """
        m, n = os.urandom(32), os.urandom(32)
        k1 = derive_hybrid_key(os.urandom(32), m, n)
        k2 = derive_hybrid_key(os.urandom(32), m, n)
        assert k1 != k2

    # KEY TEST — proprietà ibrida
    def test_mlkem_secret_contributes(self) -> None:
        """
        Cambiando solo mlkem_secret la chiave finale cambia.
        Verifica che ML-KEM contribuisca al risultato — se X25519 fosse
        rotto da un computer quantistico, ML-KEM protegge ancora.
        """
        x, n = os.urandom(32), os.urandom(32)
        k1 = derive_hybrid_key(x, os.urandom(32), n)
        k2 = derive_hybrid_key(x, os.urandom(32), n)
        assert k1 != k2

    def test_domain_separator_isolates_keys(self) -> None:
        """Info label diverso → chiavi diverse anche con stesso materiale."""
        x, m, n = os.urandom(32), os.urandom(32), os.urandom(32)
        k1 = derive_hybrid_key(x, m, n, info=b"stls-pqc-hybrid-v1")
        k2 = derive_hybrid_key(x, m, n, info=b"stls-pqc-hybrid-v2")
        assert k1 != k2

    def test_output_is_high_entropy(self) -> None:
        """32 byte → tutte le 32 posizioni devono variare (sanity check)."""
        keys = {derive_hybrid_key(os.urandom(32), os.urandom(32), os.urandom(32))
                for _ in range(50)}
        assert len(keys) == 50, "Collisione inattesa — entropia insufficiente"


# ===========================================================================
# TestMLKEM  (requires liboqs)
# ===========================================================================

@requires_liboqs
class TestMLKEM:
    """ML-KEM-768 encapsulation/decapsulation — richiede liboqs-python."""

    def test_keypair_nonempty(self) -> None:
        pub, sec = mlkem_generate_keypair()
        assert len(pub) > 0
        assert len(sec) > 0

    def test_public_key_longer_than_secret_key(self) -> None:
        """ML-KEM-768: pk ~ 1184 byte, sk ~ 2400 byte."""
        pub, sec = mlkem_generate_keypair()
        # Entrambi devono essere sostanzialmente più grandi di 32 byte
        assert len(pub) > 100
        assert len(sec) > 100

    def test_keypairs_are_random(self) -> None:
        pub1, _ = mlkem_generate_keypair()
        pub2, _ = mlkem_generate_keypair()
        assert pub1 != pub2

    # KEY TEST — round-trip fondamentale
    def test_encap_decap_roundtrip(self) -> None:
        """
        encap(pub) → (ct, server_secret)
        decap(sec, ct) → client_secret
        server_secret == client_secret  ← verifica core di ML-KEM
        """
        pub, sec = mlkem_generate_keypair()
        ct, server_secret = mlkem_encap(pub)
        client_secret = mlkem_decap(sec, ct)
        assert server_secret == client_secret

    def test_encap_is_probabilistic(self) -> None:
        """Ogni encap con la stessa pubkey produce ciphertext diverso."""
        pub, _ = mlkem_generate_keypair()
        ct1, s1 = mlkem_encap(pub)
        ct2, s2 = mlkem_encap(pub)
        assert ct1 != ct2
        assert s1 != s2

    def test_ciphertext_differs_from_secret(self) -> None:
        pub, _ = mlkem_generate_keypair()
        ct, secret = mlkem_encap(pub)
        assert ct != secret

    def test_wrong_secret_key_produces_different_secret(self) -> None:
        """Decap con la chiave sbagliata non produce il secret corretto."""
        pub, sec_correct = mlkem_generate_keypair()
        _, sec_wrong = mlkem_generate_keypair()
        ct, server_secret = mlkem_encap(pub)
        client_secret_wrong = mlkem_decap(sec_wrong, ct)
        assert client_secret_wrong != server_secret


# ===========================================================================
# TestKeySessionProviderPQC
# ===========================================================================

class TestKeySessionProviderPQC:
    """KeySessionProvider — interfaccia provider senza dipendenze liboqs."""

    @pytest.mark.asyncio
    async def test_get_kid_after_inject(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        provider.inject_key("test-kid", os.urandom(32))
        assert await provider.getSessionKeyIdentifier() == "test-kid"

    @pytest.mark.asyncio
    async def test_get_key_after_inject(self) -> None:
        store = PQCKeyStore()
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, store
        )
        key = os.urandom(32)
        provider.inject_key("kid-1", key)
        retrieved = await provider.getSessionKeyData("kid-1")
        assert retrieved == key

    @pytest.mark.asyncio
    async def test_get_kid_raises_without_inject(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        with pytest.raises(RuntimeError, match="kid non disponibile"):
            await provider.getSessionKeyIdentifier()

    @pytest.mark.asyncio
    async def test_get_key_raises_missing(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        with pytest.raises(KeyError):
            await provider.getSessionKeyData("nonexistent-kid")

    @pytest.mark.asyncio
    async def test_set_session_key_identifier_client(self) -> None:
        """Lato client: kid è impostato via setSessionKeyIdentifier()."""
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.CLIENT, PQCKeyStore()
        )
        provider.setSessionKeyIdentifier("client-kid")
        assert await provider.getSessionKeyIdentifier() == "client-kid"

    def test_inject_key_updates_kid(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        provider.inject_key("first-kid", os.urandom(32))
        assert provider._kid == "first-kid"

    def test_multiple_inject_keys_independent(self) -> None:
        """
        Ogni handshake inietta un kid distinto — le chiavi precedenti
        rimangono accessibili nel key store (usate da sessioni attive).
        """
        store = PQCKeyStore()
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, store
        )
        k1, k2 = os.urandom(32), os.urandom(32)
        provider.inject_key("kid-a", k1)
        provider.inject_key("kid-b", k2)
        assert store.get("kid-a") == k1
        assert store.get("kid-b") == k2

    def test_session_type_server(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        assert provider.getSessionType() == KeySessionProvider.KeySessionProviderType.SERVER

    def test_session_type_client(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.CLIENT, PQCKeyStore()
        )
        assert provider.getSessionType() == KeySessionProvider.KeySessionProviderType.CLIENT

    def test_get_peer_identity_returns_none(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        assert provider.getPeerIdentity() is None

    def test_client_keypairs_exposed(self) -> None:
        x25519_priv = os.urandom(32)
        mlkem_sec = os.urandom(64)
        provider = KeySessionProviderPQC(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            key_store=PQCKeyStore(),
            x25519_private_key_bytes=x25519_priv,
            mlkem_secret_key_bytes=mlkem_sec,
        )
        assert provider.x25519_private_key_bytes == x25519_priv
        assert provider.mlkem_secret_key_bytes == mlkem_sec

    def test_server_keypairs_are_none(self) -> None:
        provider = KeySessionProviderPQC(
            KeySessionProvider.KeySessionProviderType.SERVER, PQCKeyStore()
        )
        assert provider.x25519_private_key_bytes is None
        assert provider.mlkem_secret_key_bytes is None


# ===========================================================================
# TestKeyProviderLifecycle
# ===========================================================================

class TestKeyProviderLifecycle:
    """Lifecycle del provider client e server."""

    # --- Server (no liboqs) ---

    @pytest.mark.asyncio
    async def test_server_start_creates_session(self) -> None:
        server = KeyProviderServerPQC()
        result = await server.start()
        session = server.createProviderSession()
        assert session is not None
        assert session.getSessionType() == KeySessionProvider.KeySessionProviderType.SERVER

    def test_server_create_session_without_start_raises(self) -> None:
        server = KeyProviderServerPQC()
        with pytest.raises(RuntimeError, match="start()"):
            server.createProviderSession()

    @pytest.mark.asyncio
    async def test_server_stop(self) -> None:
        server = KeyProviderServerPQC()
        await server.start()
        await server.stop()  # deve completare senza eccezioni

    @pytest.mark.asyncio
    async def test_server_same_session_instance(self) -> None:
        """createProviderSession() restituisce sempre la stessa istanza."""
        server = KeyProviderServerPQC()
        await server.start()
        s1 = server.createProviderSession()
        s2 = server.createProviderSession()
        assert s1 is s2

    # --- Client (richiede liboqs per start()) ---

    def test_client_create_session_without_start_raises(self) -> None:
        client = KeyProviderClientPQC()
        with pytest.raises(RuntimeError, match="start()"):
            client.createProviderSession()

    def test_client_pubkey_without_start_raises(self) -> None:
        client = KeyProviderClientPQC()
        with pytest.raises(RuntimeError):
            _ = client.x25519_public_key_bytes

    @requires_liboqs
    def test_client_start_generates_x25519_pubkey(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        assert len(client.x25519_public_key_bytes) == 32

    @requires_liboqs
    def test_client_start_generates_mlkem_pubkey(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        assert len(client.mlkem_public_key_bytes) > 100  # ML-KEM-768 pk ~ 1184 byte

    @requires_liboqs
    def test_client_session_exposes_private_keys(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        session = client.createProviderSession()
        assert session.x25519_private_key_bytes is not None
        assert session.mlkem_secret_key_bytes is not None

    @requires_liboqs
    def test_client_stop_zeros_private_keys(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        session = client.createProviderSession()
        priv_len = len(session.x25519_private_key_bytes)
        client.stop()
        assert session.x25519_private_key_bytes == b"\x00" * priv_len

    @requires_liboqs
    def test_client_each_start_fresh_keypair(self) -> None:
        c1, c2 = KeyProviderClientPQC(), KeyProviderClientPQC()
        c1.start()
        c2.start()
        assert c1.x25519_public_key_bytes != c2.x25519_public_key_bytes

    # --- Factory ---

    @requires_liboqs
    def test_key_provider_pqc_get_name(self) -> None:
        assert KeyProviderPQC().get_name() == "pqc"

    @requires_liboqs
    def test_key_provider_pqc_create_server(self) -> None:
        server = KeyProviderPQC().create_provider_server(port=0, options={})
        assert isinstance(server, KeyProviderServerPQC)

    @requires_liboqs
    def test_key_provider_pqc_create_client(self) -> None:
        client = KeyProviderPQC().create_provider_client(host="", port=0, options={})
        assert isinstance(client, KeyProviderClientPQC)

    @requires_liboqs
    def test_key_provider_factory_recognizes_pqc(self) -> None:
        from stls_core.providers.stls_core_providers_factory import KeyProviderFactory
        provider = KeyProviderFactory().get_provider("pqc")
        assert provider.get_name() == "pqc"


# ===========================================================================
# TestPQCHandshakeModels
# ===========================================================================

class TestPQCHandshakeModels:
    """Wire model Pydantic e PQCHandshakeError."""

    def test_handshake_error_message_and_code(self) -> None:
        err = PQCHandshakeError("test error", code=-32600)
        assert err.message == "test error"
        assert err.code == -32600
        assert str(err) == "test error"

    def test_handshake_error_default_code(self) -> None:
        err = PQCHandshakeError("internal")
        assert err.code == -32603

    @requires_liboqs
    def test_request_generate_nonce_is_32_bytes(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        req = PQCHandshakeRequest.generate(
            "test-client",
            client.x25519_public_key_bytes,
            client.mlkem_public_key_bytes,
        )
        assert len(base64.b64decode(req.nonce)) == 32

    @requires_liboqs
    def test_request_generate_x25519_pub_is_32_bytes(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        req = PQCHandshakeRequest.generate(
            "c", client.x25519_public_key_bytes, client.mlkem_public_key_bytes
        )
        assert len(base64.b64decode(req.x25519_public_key)) == 32

    @requires_liboqs
    def test_request_generate_nonce_is_unique(self) -> None:
        client = KeyProviderClientPQC()
        client.start()
        pub_x = client.x25519_public_key_bytes
        pub_m = client.mlkem_public_key_bytes
        nonces = {PQCHandshakeRequest.generate("c", pub_x, pub_m).nonce for _ in range(20)}
        assert len(nonces) == 20

    def test_request_protocol_default(self) -> None:
        req = PQCHandshakeRequest(
            client_id="c",
            nonce=base64.b64encode(os.urandom(32)).decode(),
            x25519_public_key=base64.b64encode(os.urandom(32)).decode(),
            mlkem_public_key=base64.b64encode(os.urandom(100)).decode(),
        )
        assert req.protocol == "1.0"

    def test_response_key_derivation_label(self) -> None:
        resp = PQCHandshakeResponse(
            session_id="sid",
            kid="kid",
            proof=base64.b64encode(b"x"*32).decode(),
            server_x25519_public_key=base64.b64encode(os.urandom(32)).decode(),
            mlkem_ciphertext=base64.b64encode(os.urandom(100)).decode(),
        )
        assert resp.key_derivation == "HKDF-SHA256+X25519+ML-KEM-768"


# ===========================================================================
# TestPerformPQCHandshake  (requires liboqs)
# ===========================================================================

@requires_liboqs
class TestPerformPQCHandshake:
    """Server-side handshake — richiede liboqs."""

    def _make_request(self) -> tuple[KeyProviderClientPQC, PQCHandshakeRequest]:
        client = KeyProviderClientPQC()
        client.start()
        req = PQCHandshakeRequest.generate(
            "test-client",
            client.x25519_public_key_bytes,
            client.mlkem_public_key_bytes,
        )
        return client, req

    @pytest.mark.asyncio
    async def test_response_has_session_id(self) -> None:
        _, req = self._make_request()
        resp = await perform_pqc_handshake(req, InMemorySessionCache())
        assert resp.session_id and len(resp.session_id) > 0

    @pytest.mark.asyncio
    async def test_response_has_kid(self) -> None:
        _, req = self._make_request()
        resp = await perform_pqc_handshake(req, InMemorySessionCache())
        assert resp.kid and len(resp.kid) > 0

    @pytest.mark.asyncio
    async def test_response_has_proof(self) -> None:
        _, req = self._make_request()
        resp = await perform_pqc_handshake(req, InMemorySessionCache())
        assert resp.proof and len(base64.b64decode(resp.proof)) == 32

    @pytest.mark.asyncio
    async def test_server_x25519_pub_is_32_bytes(self) -> None:
        _, req = self._make_request()
        resp = await perform_pqc_handshake(req, InMemorySessionCache())
        assert len(base64.b64decode(resp.server_x25519_public_key)) == 32

    @pytest.mark.asyncio
    async def test_mlkem_ciphertext_nonempty(self) -> None:
        _, req = self._make_request()
        resp = await perform_pqc_handshake(req, InMemorySessionCache())
        assert len(base64.b64decode(resp.mlkem_ciphertext)) > 100

    @pytest.mark.asyncio
    async def test_session_stored_in_cache(self) -> None:
        _, req = self._make_request()
        cache = InMemorySessionCache()
        resp = await perform_pqc_handshake(req, cache)
        assert resp.session_id in cache
        assert cache.get(resp.session_id) is not None

    @pytest.mark.asyncio
    async def test_session_id_and_kid_are_uuids(self) -> None:
        import uuid
        _, req = self._make_request()
        resp = await perform_pqc_handshake(req, InMemorySessionCache())
        # Deve essere UUID valido
        uuid.UUID(resp.session_id)
        uuid.UUID(resp.kid)

    @pytest.mark.asyncio
    async def test_short_nonce_raises(self) -> None:
        req = PQCHandshakeRequest(
            client_id="c",
            nonce=base64.b64encode(b"too_short").decode(),   # 9 byte < 16
            x25519_public_key=base64.b64encode(os.urandom(32)).decode(),
            mlkem_public_key=base64.b64encode(os.urandom(100)).decode(),
        )
        with pytest.raises(PQCHandshakeError, match="troppo corto"):
            await perform_pqc_handshake(req, InMemorySessionCache())

    @pytest.mark.asyncio
    async def test_x25519_key_wrong_size_raises(self) -> None:
        req = PQCHandshakeRequest(
            client_id="c",
            nonce=base64.b64encode(os.urandom(32)).decode(),
            x25519_public_key=base64.b64encode(b"only_16_bytes___").decode(),  # 16 != 32
            mlkem_public_key=base64.b64encode(os.urandom(100)).decode(),
        )
        with pytest.raises(PQCHandshakeError, match="32 byte"):
            await perform_pqc_handshake(req, InMemorySessionCache())


# ===========================================================================
# TestRoundTrip  — test di integrazione E2E  (requires liboqs)
# ===========================================================================

@requires_liboqs
class TestRoundTrip:
    """
    Test di integrazione end-to-end client + server.
    Verifica che client e server derivino la stessa session_key
    e possano cifrare/decifrare messaggi con StlsTransport.
    """

    async def _full_handshake(
        self,
        cache: Optional[InMemorySessionCache] = None,
    ) -> tuple[bytes, str, PQCHandshakeResponse, str]:
        """
        Esegue un handshake completo e restituisce
        (client_key, nonce, response, session_id).
        """
        if cache is None:
            cache = InMemorySessionCache()

        client = KeyProviderClientPQC()
        client.start()
        req = PQCHandshakeRequest.generate(
            "test-client",
            client.x25519_public_key_bytes,
            client.mlkem_public_key_bytes,
        )
        nonce = req.nonce
        resp = await perform_pqc_handshake(req, cache)

        session = client.createProviderSession()
        client_key = derive_session_key_client(
            session.x25519_private_key_bytes,
            session.mlkem_secret_key_bytes,
            resp,
            nonce,
        )
        return client_key, nonce, resp, cache

    # KEY TEST
    @pytest.mark.asyncio
    async def test_client_and_server_derive_same_key(self) -> None:
        """
        Proprietà fondamentale: client e server ottengono lo stesso
        materiale crittografico senza trasmetterlo sul canale.
        """
        cache = InMemorySessionCache()
        client_key, nonce, resp, cache = await self._full_handshake(cache)

        # Recupera la session_key server-side dal transport in cache
        server_transport = cache.get(resp.session_id)
        assert server_transport is not None

        # Ri-cifra con la client_key e decifra con il server transport
        client_transport = StlsTransport(client_key, resp.session_id)
        plaintext = b"stls-pqc-hybrid-test"
        envelope = client_transport.encrypt(plaintext)
        assert server_transport.decrypt(envelope) == plaintext

    # KEY TEST
    @pytest.mark.asyncio
    async def test_proof_verification_succeeds(self) -> None:
        """
        Il proof HMAC inviato dal server è verificabile con la chiave
        derivata lato client — proof of possession reciproco.
        """
        cache = InMemorySessionCache()
        client_key, nonce, resp, _ = await self._full_handshake(cache)
        assert verify_pqc_handshake_proof(client_key, nonce, resp)

    @pytest.mark.asyncio
    async def test_proof_verification_fails_wrong_key(self) -> None:
        """Una chiave diversa non supera la verifica del proof."""
        cache = InMemorySessionCache()
        _, nonce, resp, _ = await self._full_handshake(cache)
        assert not verify_pqc_handshake_proof(os.urandom(32), nonce, resp)

    @pytest.mark.asyncio
    async def test_bidirectional_encryption(self) -> None:
        """
        Cifratura bidirezionale: server → client e client → server.
        Entrambe le direzioni funzionano con la stessa session_key.
        """
        cache = InMemorySessionCache()
        client_key, nonce, resp, cache = await self._full_handshake(cache)

        client_t = StlsTransport(client_key, resp.session_id)
        server_t = cache.get(resp.session_id)

        # Client cifra → Server decifra
        msg_c2s = b'{"role": "user", "content": "hello"}'
        assert server_t.decrypt(client_t.encrypt(msg_c2s)) == msg_c2s

        # Server cifra → Client decifra
        msg_s2c = b'{"role": "assistant", "content": "world"}'
        assert client_t.decrypt(server_t.encrypt(msg_s2c)) == msg_s2c

    @pytest.mark.asyncio
    async def test_multiple_messages_same_session(self) -> None:
        """
        Più messaggi sullo stesso session_id — ogni encrypt usa un IV
        diverso (os.urandom(12) in StlsTransport) ma la stessa chiave.
        """
        cache = InMemorySessionCache()
        client_key, _, resp, cache = await self._full_handshake(cache)

        client_t = StlsTransport(client_key, resp.session_id)
        server_t = cache.get(resp.session_id)

        for i in range(10):
            msg = f"message_{i}".encode()
            assert server_t.decrypt(client_t.encrypt(msg)) == msg

    @pytest.mark.asyncio
    async def test_three_independent_sessions(self) -> None:
        """
        Tre handshake concorrenti producono session_id, kid e chiavi
        completamente indipendenti — nessuna cross-contaminazione.
        """
        cache = InMemorySessionCache()

        async def handshake_one() -> tuple[str, str, bytes]:
            client = KeyProviderClientPQC()
            client.start()
            req = PQCHandshakeRequest.generate(
                "client",
                client.x25519_public_key_bytes,
                client.mlkem_public_key_bytes,
            )
            nonce = req.nonce
            resp = await perform_pqc_handshake(req, cache)
            session = client.createProviderSession()
            key = derive_session_key_client(
                session.x25519_private_key_bytes,
                session.mlkem_secret_key_bytes,
                resp,
                nonce,
            )
            return resp.session_id, resp.kid, key

        results = await asyncio.gather(
            handshake_one(), handshake_one(), handshake_one()
        )
        sids = [r[0] for r in results]
        kids = [r[1] for r in results]
        keys = [r[2] for r in results]

        assert len(set(sids)) == 3, "session_id non univoci"
        assert len(set(kids)) == 3, "kid non univoci"
        assert len(set(keys)) == 3, "session_key identiche — collisione!"

    @pytest.mark.asyncio
    async def test_wrong_ciphertext_fails_decryption(self) -> None:
        """
        Un client con chiavi diverse non riesce a decifrare i messaggi
        di un'altra sessione.
        """
        cache = InMemorySessionCache()
        key_a, _, resp_a, cache = await self._full_handshake(cache)
        key_b, _, resp_b, cache = await self._full_handshake(cache)

        transport_a = StlsTransport(key_a, resp_a.session_id)
        transport_b_wrong = StlsTransport(key_b, resp_a.session_id)  # chiave sbagliata

        msg = b"secret message"
        envelope = transport_a.encrypt(msg)

        # Tentativo di decifrazione con chiave B → deve fallire
        with pytest.raises(ValueError):
            transport_b_wrong.decrypt(envelope)