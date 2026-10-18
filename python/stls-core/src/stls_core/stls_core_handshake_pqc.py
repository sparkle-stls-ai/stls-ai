"""
stls_core_handshake_pqc.py
--------------------------
Handshake STLS con key agreement ibrido X25519 + ML-KEM-768.

Endpoint dedicato: POST /stls/pqc/handshake
(distinto da POST /stls/handshake, attivo quando KEY_PROVIDER_NAME=pqc)

Flusso completo:

    Client                                          Server
      │                                               │
      │  [KeyProviderClientPQC.start()]               │
      │  genera keypair X25519 + ML-KEM-768           │
      │                                               │
      │  POST /stls/pqc/handshake                     │
      │  { client_id, nonce,              ─────────►  │  perform_pqc_handshake()
      │    x25519_pub, mlkem_pub }                     │    ① genera keypair X25519 server
      │                                               │    ② X25519 DH → x25519_secret
      │                                               │    ③ ML-KEM encap → (ct, mlkem_secret)
      │                                               │    ④ HKDF(x25519‖mlkem, salt=nonce)
      │                                               │       → session_key
      │                                               │    ⑤ proof = HMAC(session_key, nonce)
      │                                               │    ⑥ session_cache.put(sid, kid, key)
      │  ◄─────────────────────────────────────────   │
      │  { session_id, kid, proof,                    │
      │    server_x25519_pub, mlkem_ct }              │
      │                                               │
      │  derive_session_key_client()                  │
      │    X25519 DH(client_priv, server_pub)         │
      │    ML-KEM decap(client_sec, mlkem_ct)         │
      │    HKDF(x25519‖mlkem, salt=nonce)             │
      │    → session_key                              │
      │                                               │
      │  verify_pqc_handshake_proof()                 │
      │    assert HMAC(own_key, nonce) == proof  ✓    │
      │                                               │
      │  POST /stls/secure-a2a (cifrato)              │
      │  X-STLS-Session-ID: session_id  ───────────►  │  SessionCache.get(session_id) → transport

Garanzie (ereditate dall'handshake standard + quantum-safety):
  - session_id generato dal server → non controllabile dal client
  - proof dimostra possesso reciproco della stessa chiave derivata
  - nessun key material in transito (solo pubkey + ciphertext KEM)
  - ML-KEM-768: resistente a Shor's algorithm (NIST Level 3)
  - X25519: fallback se ML-KEM avesse vulnerabilità ignota
  - nonce come salt HKDF: freshness garantita per ogni sessione
"""

from __future__ import annotations

import base64
import hmac as _hmac
import logging
import os
import uuid
from hashlib import sha256

from pydantic import BaseModel, Field

from .stls_core_constants import STLS_VERSION, HANDSHAKE_PQC_PATH
from .stls_core_session import SessionCacheBase
from .providers.stls_core_providers_pqc import (
    mlkem_decap,
    mlkem_encap,
    x25519_exchange,
    x25519_generate_keypair,
    derive_hybrid_key,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------

class PQCHandshakeRequest(BaseModel):
    """
    Corpo della richiesta POST /stls/pqc/handshake.

    Estende il formato HandshakeRequest standard con le pubkey KEM del client.

    Attributes:
        client_id:          Identificativo del client (logging/audit).
        nonce:              32 byte random base64 — HMAC proof + HKDF salt.
        x25519_public_key:  Pubkey X25519 del client (32 byte, base64).
        mlkem_public_key:   Pubkey ML-KEM-768 del client (base64).
        protocol:           Versione protocollo STLS (default "1.0").
    """
    client_id: str = Field(..., description="Identificativo del client")
    nonce: str = Field(..., description="32 byte random base64 — monouso")
    x25519_public_key: str = Field(..., description="Pubkey X25519 client, 32 byte base64")
    mlkem_public_key: str = Field(..., description="Pubkey ML-KEM-768 client, base64")
    protocol: str = Field(default=STLS_VERSION)

    @classmethod
    def generate(
        cls,
        client_id: str,
        x25519_public_key_bytes: bytes,
        mlkem_public_key_bytes: bytes,
    ) -> "PQCHandshakeRequest":
        """
        Factory: genera una PQCHandshakeRequest con nonce random.

        Usata dal client STLS prima di chiamare POST /stls/pqc/handshake.

        Args:
            client_id:               Identificativo del client.
            x25519_public_key_bytes: Pubkey X25519 (da KeyProviderClientPQC).
            mlkem_public_key_bytes:  Pubkey ML-KEM-768 (da KeyProviderClientPQC).
        """
        return cls(
            client_id=client_id,
            nonce=base64.b64encode(os.urandom(32)).decode("utf-8"),
            x25519_public_key=base64.b64encode(x25519_public_key_bytes).decode("utf-8"),
            mlkem_public_key=base64.b64encode(mlkem_public_key_bytes).decode("utf-8"),
        )


class PQCHandshakeResponse(BaseModel):
    """
    Corpo della risposta POST /stls/pqc/handshake.

    Attributes:
        session_id:               UUID generato dal server — usato in X-STLS-Session-ID.
        kid:                      UUID della chiave derivata — usato in X-STLS-Kid.
        proof:                    HMAC-SHA256(session_key, nonce) base64.
        server_x25519_public_key: Pubkey X25519 del server (32 byte, base64).
        mlkem_ciphertext:         Ciphertext ML-KEM-768 (base64).
        protocol:                 Versione protocollo STLS.
        encryption:               Algoritmo di cifratura.
        hmac:                     Algoritmo HMAC.
        key_derivation:           Schema di derivazione chiave.
    """
    session_id: str
    kid: str
    proof: str = Field(..., description="HMAC-SHA256(session_key, nonce) base64")
    server_x25519_public_key: str = Field(..., description="Pubkey X25519 server, 32 byte base64")
    mlkem_ciphertext: str = Field(..., description="Ciphertext ML-KEM-768, base64")
    protocol: str = Field(default=STLS_VERSION)
    encryption: str = Field(default="AES-256-GCM")
    hmac: str = Field(default="HMAC-SHA256")
    key_derivation: str = Field(default="HKDF-SHA256+X25519+ML-KEM-768")


# ---------------------------------------------------------------------------
# PQCHandshakeError
# ---------------------------------------------------------------------------

class PQCHandshakeError(Exception):
    """
    Sollevata da perform_pqc_handshake() in caso di errore.

    Attributes:
        message: Descrizione dell'errore.
        code:    Codice JSON-RPC (-32603 internal, -32600 bad request).
    """
    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Server-side: perform_pqc_handshake()
# ---------------------------------------------------------------------------

async def perform_pqc_handshake(
    request: PQCHandshakeRequest,
    session_cache: SessionCacheBase,
) -> PQCHandshakeResponse:
    """
    Esegue il lato server dell'handshake PQC.

    A differenza di perform_handshake(), non richiede un key_session_provider
    perché la session_key è derivata localmente via KEM — nessun servizio
    esterno coinvolto. La chiave derivata viene salvata direttamente nella
    session_cache con session_id e kid generati server-side.

    Flusso:
        1. Decodifica e valida nonce e pubkey del client.
        2. Genera keypair X25519 server-side (fresh per ogni handshake).
        3. X25519 DH: x25519_secret = DH(server_priv, client_x25519_pub).
        4. ML-KEM-768 encap: (mlkem_ct, mlkem_secret) = encap(client_mlkem_pub).
        5. HKDF: session_key = HKDF(x25519_secret ‖ mlkem_secret, salt=nonce).
        6. Genera session_id (uuid4) e kid (uuid4).
        7. Calcola proof = HMAC-SHA256(session_key, nonce).
        8. Registra la sessione: session_cache.put(session_id, kid, session_key).
        9. Restituisce PQCHandshakeResponse con server_x25519_pub + mlkem_ct.

    Args:
        request:       PQCHandshakeRequest validata da Pydantic.
        session_cache: SessionCacheBase condivisa con il middleware cifrato.

    Returns:
        PQCHandshakeResponse con session_id, kid, proof e materiale KEM server.

    Raises:
        PQCHandshakeError: Se le pubkey sono malformate o un'operazione KEM fallisce.
    """

    # ── 1. Decodifica e valida input ──────────────────────────────────────────
    try:
        nonce = base64.b64decode(request.nonce)
        if len(nonce) < 16:
            raise PQCHandshakeError(
                f"Nonce troppo corto: {len(nonce)} byte (minimo 16)",
                code=-32600,
            )
    except PQCHandshakeError:
        raise
    except Exception as exc:
        raise PQCHandshakeError(f"Nonce non è base64 valido: {exc}", code=-32600) from exc

    try:
        client_x25519_pub = base64.b64decode(request.x25519_public_key)
        if len(client_x25519_pub) != 32:
            raise PQCHandshakeError(
                f"x25519_public_key deve essere 32 byte, "
                f"ricevuto {len(client_x25519_pub)}",
                code=-32600,
            )
    except PQCHandshakeError:
        raise
    except Exception as exc:
        raise PQCHandshakeError(
            f"x25519_public_key non è base64 valido: {exc}", code=-32600
        ) from exc

    try:
        client_mlkem_pub = base64.b64decode(request.mlkem_public_key)
    except Exception as exc:
        raise PQCHandshakeError(
            f"mlkem_public_key non è base64 valido: {exc}", code=-32600
        ) from exc

    session_id = str(uuid.uuid4())
    kid = str(uuid.uuid4())   # kid PQC = UUID fresco, distinto per ogni sessione

    logger.info(
        "PQC Handshake avviato: client_id=%s session_id=%s",
        request.client_id, session_id,
    )

    # ── 2. Genera keypair X25519 server-side ──────────────────────────────────
    try:
        server_x25519_priv, server_x25519_pub = x25519_generate_keypair()
    except Exception as exc:
        raise PQCHandshakeError(f"Errore generazione keypair X25519: {exc}") from exc

    # ── 3. X25519 DH exchange ─────────────────────────────────────────────────
    try:
        x25519_secret = x25519_exchange(server_x25519_priv, client_x25519_pub)
    except Exception as exc:
        raise PQCHandshakeError(f"X25519 DH exchange fallito: {exc}") from exc

    # ── 4. ML-KEM-768 encapsulation ───────────────────────────────────────────
    try:
        mlkem_ciphertext, mlkem_secret = mlkem_encap(client_mlkem_pub)
    except Exception as exc:
        raise PQCHandshakeError(f"ML-KEM encapsulation fallita: {exc}") from exc

    # ── 5. Deriva session_key ibrida via HKDF ─────────────────────────────────
    try:
        session_key = derive_hybrid_key(
            x25519_secret=x25519_secret,
            mlkem_secret=mlkem_secret,
            nonce=nonce,
        )
    except Exception as exc:
        raise PQCHandshakeError(f"HKDF derivazione fallita: {exc}") from exc

    # ── 6. Calcola proof = HMAC-SHA256(session_key, nonce) ────────────────────
    proof_bytes = _hmac.new(session_key, nonce, sha256).digest()
    proof = base64.b64encode(proof_bytes).decode("utf-8")

    # ── 7. Registra sessione in cache ─────────────────────────────────────────
    try:
        session_cache.put(session_id, kid, session_key)
    except Exception as exc:
        raise PQCHandshakeError(f"Errore registrazione sessione in cache: {exc}") from exc

    logger.info(
        "PQC Handshake completato: session_id=%s kid=%s client_id=%s key_len=%d",
        session_id, kid, request.client_id, len(session_key),
    )

    return PQCHandshakeResponse(
        session_id=session_id,
        kid=kid,
        proof=proof,
        server_x25519_public_key=base64.b64encode(server_x25519_pub).decode("utf-8"),
        mlkem_ciphertext=base64.b64encode(mlkem_ciphertext).decode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Client-side: derive_session_key_client() + verify_pqc_handshake_proof()
# ---------------------------------------------------------------------------

def derive_session_key_client(
    client_x25519_private_key_bytes: bytes,
    client_mlkem_secret_key_bytes: bytes,
    response: PQCHandshakeResponse,
    nonce: str,
) -> bytes:
    """
    Deriva la session_key lato client dopo aver ricevuto la PQCHandshakeResponse.

    Esegue la parte client del KEM ibrido: X25519 DH con la pubkey del server +
    ML-KEM decapsulation con il ciphertext del server. Applica la stessa HKDF
    del server con lo stesso nonce → session_key identica.

    Args:
        client_x25519_private_key_bytes:  Chiave privata X25519 (da KeyProviderClientPQC).
        client_mlkem_secret_key_bytes:    Secret key ML-KEM-768 (da KeyProviderClientPQC).
        response:                         PQCHandshakeResponse ricevuta dal server.
        nonce:                            Nonce inviato nella PQCHandshakeRequest (base64).

    Returns:
        32 byte session_key — deve coincidere con quella derivata dal server.
        Da passare a StlsTransport(session_key, session_id) per iniziare
        la comunicazione cifrata.
    """
    nonce_bytes = base64.b64decode(nonce)
    server_x25519_pub = base64.b64decode(response.server_x25519_public_key)
    mlkem_ct = base64.b64decode(response.mlkem_ciphertext)

    # X25519 DH (client side) — speculare al server: stesso shared secret
    x25519_secret = x25519_exchange(
        client_x25519_private_key_bytes,
        server_x25519_pub,
    )

    # ML-KEM decapsulation — recupera mlkem_secret dal ciphertext
    mlkem_secret = mlkem_decap(client_mlkem_secret_key_bytes, mlkem_ct)

    # HKDF ibrido — stessa derivazione del server, stesse input → stessa chiave
    session_key = derive_hybrid_key(
        x25519_secret=x25519_secret,
        mlkem_secret=mlkem_secret,
        nonce=nonce_bytes,
    )

    logger.debug("[PQC client] session_key derivata: len=%d", len(session_key))
    return session_key


def verify_pqc_handshake_proof(
    session_key: bytes,
    nonce: str,
    response: PQCHandshakeResponse,
) -> bool:
    """
    Verifica lato client che il server abbia derivato la stessa session_key.

    Calcola HMAC-SHA256(own_key, nonce) e confronta con il proof del server.
    Se coincidono, il server ha eseguito correttamente il KEM e ha la stessa
    chiave — proof of possession senza trasmettere la chiave.

    Args:
        session_key: Session_key derivata lato client (da derive_session_key_client()).
        nonce:       Nonce inviato nella PQCHandshakeRequest (base64).
        response:    PQCHandshakeResponse ricevuta dal server.

    Returns:
        True se il proof è valido, False altrimenti.

    Raises:
        ValueError: Se nonce o proof non sono base64 validi.
    """
    nonce_bytes = base64.b64decode(nonce)
    expected = _hmac.new(session_key, nonce_bytes, sha256).digest()
    received = base64.b64decode(response.proof)
    return _hmac.compare_digest(expected, received)