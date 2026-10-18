"""
stls_core_handshake.py
----------------------
Handshake STLS con proof-of-key-possession tramite HMAC challenge/response.

La chiave non viaggia mai sul canale. Il flusso è:

    Client                              Server
      │                                   │
      │  POST /stls/handshake             │
      │  { client_id, nonce }  ─────────► │
      │                                   │  kid  = provider.getSessionKeyIdentifier()
      │                                   │  key  = provider.getSessionKeyData(kid)
      │                                   │  sid  = uuid4()               ← server genera
      │                                   │  proof= HMAC-SHA256(key, nonce)
      │                                   │  SessionCache.put(sid, transport)
      │  ◄─────────────────────────────   │
      │  { session_id, kid, proof }       │
      │                                   │
      │  own_proof = HMAC(own_key, nonce) │
      │  assert own_proof == proof        │ ← mutual proof of possession
      │                                   │
      │  POST /stls/a2a (cifrato)         │
      │  X-STLS-Session-ID: sid ────────► │  SessionCache.get(sid) → transport

Garanzie:
  - session_id generato dal server → non controllabile dal client
  - proof dimostra che server possiede la stessa chiave del client
  - nessun key material in transito in nessuna direzione
  - replay attack mitigato: nonce monouso 32 byte random per handshake

Il layer HTTP (stls-a2a, stls-mcp) registra POST /stls/handshake e chiama
perform_handshake(). La verifica lato client è in verify_handshake_proof().
"""

from __future__ import annotations

import asyncio
import base64
import hmac as _hmac
import inspect
import logging
import os
import uuid
from hashlib import sha256
from typing import Any, Optional

from pydantic import BaseModel, Field

from .stls_core_constants import STLS_VERSION, HANDSHAKE_PATH
from .stls_core_crypto import StlsTransport
from .stls_core_session import SessionCache, SessionCacheBase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modelli Pydantic — wire format
# ---------------------------------------------------------------------------

class HandshakeRequest(BaseModel):
    """
    Corpo della richiesta POST /stls/handshake.

    Attributes:
        client_id:  Identificativo del client (es. "mcp-client-1").
                    Usato per logging e audit — non autenticazione.
        nonce:      32 byte random generati dal client, base64-encoded.
                    Il server li usa per calcolare il proof HMAC.
                    Monouso: un nuovo nonce per ogni handshake.
        protocol:   Versione protocollo STLS attesa (default "1.0").
    """
    client_id: str = Field(..., description="Identificativo del client")
    nonce: str = Field(
        ...,
        description="32 byte random base64-encoded, generati dal client",
    )
    protocol: str = Field(default=STLS_VERSION)

    @classmethod
    def generate(cls, client_id: str) -> "HandshakeRequest":
        """
        Factory: genera una HandshakeRequest con nonce random.
        Usata dal client STLS prima di chiamare POST /stls/handshake.
        """
        nonce = base64.b64encode(os.urandom(32)).decode("utf-8")
        return cls(client_id=client_id, nonce=nonce)


class HandshakeResponse(BaseModel):
    """
    Corpo della risposta all'handshake.

    Attributes:
        session_id:     UUID generato dal server — identificatore della sessione.
                        Il client lo usa in X-STLS-Session-ID su ogni richiesta.
        kid:            Key identifier assegnato dal provider.
                        Il client lo usa in X-STLS-Kid per correlazione/debug.
        proof:          HMAC-SHA256(session_key, nonce) base64-encoded.
                        Il client verifica che il server possieda la stessa chiave.
        protocol:       Versione protocollo confermata dal server.
        encryption:     Algoritmo di cifratura in uso.
        hmac:           Algoritmo HMAC in uso.
        key_derivation: Schema di derivazione chiave in uso.
    """
    session_id: str
    kid: str
    proof: str = Field(..., description="HMAC-SHA256(key, nonce) base64-encoded")
    protocol: str = Field(default=STLS_VERSION)
    encryption: str = Field(default="AES-256-GCM")
    hmac: str = Field(default="HMAC-SHA256")
    key_derivation: str = Field(default="HKDF-Expand-SHA256")


# ---------------------------------------------------------------------------
# HandshakeError
# ---------------------------------------------------------------------------

class HandshakeError(Exception):
    """
    Sollevata da perform_handshake() in caso di errore.

    Attributes:
        message: Descrizione dell'errore.
        code:    Codice JSON-RPC (-32603 internal, -32600 bad request).
    """
    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Server-side: perform_handshake()
# ---------------------------------------------------------------------------

async def perform_handshake(
    request: HandshakeRequest,
    key_session_provider: Any,
    session_cache: SessionCacheBase,
) -> HandshakeResponse:
    """
    Esegue il lato server dell'handshake STLS.

    Chiamato dal route handler POST /stls/handshake in stls-a2a e stls-mcp.
    Il chiamante deserializza la request e serializza la response HTTP.

    Flusso:
        1. Decodifica il nonce dal client.
        2. Ottiene kid dal provider.
        3. Ottiene session_key dal provider tramite kid.
        4. Genera session_id (uuid4 — lato server).
        5. Calcola proof = HMAC-SHA256(session_key, nonce).
        6. Costruisce StlsTransport e registra in SessionCache.
        7. Restituisce HandshakeResponse con session_id, kid, proof.

    Args:
        request:              HandshakeRequest validata da Pydantic.
        key_session_provider: Istanza KeySessionProvider (base o arqit_py).
        session_cache:        SessionCache condivisa con il middleware.

    Returns:
        HandshakeResponse con session_id, kid e proof.

    Raises:
        HandshakeError: Se il provider non riesce a fornire una chiave valida
                        o il nonce è malformato.
    """
    # ── 1. Decodifica nonce ───────────────────────────────────────────────────
    try:
        nonce = base64.b64decode(request.nonce)
        if len(nonce) < 16:
            raise HandshakeError(
                f"Nonce troppo corto: {len(nonce)} byte (minimo 16)",
                code=-32600,
            )
    except HandshakeError:
        raise
    except Exception as exc:
        raise HandshakeError(
            f"Nonce non è base64 valido: {exc}", code=-32600
        ) from exc

    session_id = str(uuid.uuid4())

    logger.info(
        "Handshake avviato: client_id=%s session_id=%s",
        request.client_id, session_id,
    )

    # ── 2. Ottieni kid dal provider ───────────────────────────────────────────
    try:
        kid_result = key_session_provider.getSessionKeyIdentifier()
        kid = await _resolve(kid_result)
        if not kid:
            raise HandshakeError(
                "getSessionKeyIdentifier() ha restituito None o stringa vuota"
            )
    except HandshakeError:
        raise
    except Exception as exc:
        raise HandshakeError(f"Errore nel recupero del kid: {exc}") from exc

    # ── 3. Ottieni session_key dal provider ───────────────────────────────────
    try:
        raw_result = key_session_provider.getSessionKeyData(kid)
        raw_key = await _resolve_blocking(raw_result)
    except HandshakeError:
        raise
    except Exception as exc:
        raise HandshakeError(
            f"Errore nel recupero della session key (kid={kid!r}): {exc}"
        ) from exc

    if raw_key is None:
        raise HandshakeError(
            f"getSessionKeyData({kid!r}) ha restituito None"
        )

    try:
        session_key = bytes(raw_key)
    except Exception as exc:
        raise HandshakeError(
            f"Impossibile convertire session key in bytes "
            f"(type={type(raw_key).__name__}): {exc}"
        ) from exc

    if not session_key:
        raise HandshakeError("Session key vuota")

    # ── 4. Calcola proof = HMAC-SHA256(session_key, nonce) ───────────────────
    proof_bytes = _hmac.new(session_key, nonce, sha256).digest()
    proof = base64.b64encode(proof_bytes).decode("utf-8")

    # ── 5. Costruisci transport e registra in cache ───────────────────────────
    # session_cache.put() riceve i dati grezzi (session_id, kid, session_key)
    # e costruisce internamente StlsTransport — necessario per la serializzazione
    # Redis (InMemorySessionCache e RedisSessionCache condividono la stessa API).
    try:
        session_cache.put(session_id, kid, session_key)
    except Exception as exc:
        raise HandshakeError(
            f"Errore nella registrazione della sessione in cache: {exc}"
        ) from exc

    logger.info(
        "Handshake completato: session_id=%s kid=%s client_id=%s "
        "key_len=%d cache_size=%d",
        session_id, kid, request.client_id,
        len(session_key), len(session_cache),
    )

    return HandshakeResponse(
        session_id=session_id,
        kid=kid,
        proof=proof,
    )


# ---------------------------------------------------------------------------
# Client-side: verify_handshake_proof()
# ---------------------------------------------------------------------------

def verify_handshake_proof(
    session_key: bytes,
    nonce: str,
    response: HandshakeResponse,
) -> bool:
    """
    Verifica lato client che il server possieda la stessa chiave.

    Il client calcola HMAC-SHA256(own_key, nonce) e lo confronta
    con il proof ricevuto nella HandshakeResponse. Se coincidono,
    server e client hanno la stessa session_key — proof of possession
    senza trasmettere la chiave.

    Args:
        session_key: Chiave di sessione del client (da provider locale).
        nonce:       Nonce originale inviato nella HandshakeRequest (base64).
        response:    HandshakeResponse ricevuta dal server.

    Returns:
        True se il proof è valido, False altrimenti.

    Raises:
        ValueError: Se nonce o proof non sono base64 validi.
    """
    nonce_bytes = base64.b64decode(nonce)
    expected_proof = _hmac.new(session_key, nonce_bytes, sha256).digest()
    received_proof = base64.b64decode(response.proof)

    # Confronto timing-safe
    return _hmac.compare_digest(expected_proof, received_proof)


# ---------------------------------------------------------------------------
# Helpers interni
# ---------------------------------------------------------------------------

async def _resolve(result: Any) -> Any:
    """Risolve coroutine, Java future o valore diretto."""
    if inspect.isawaitable(result):
        return await result
    if hasattr(result, "get") and callable(result.get):
        return result.get()
    return result


async def _resolve_blocking(result: Any) -> Any:
    """
    Come _resolve, ma per operazioni bloccanti (I/O di rete Arqit):
    esegue nel default executor del loop per non bloccare asyncio.

    Usa l'executor di default del loop (thread pool condiviso, creato pigramente
    da asyncio) invece di istanziarne uno nuovo ad ogni chiamata: un
    ThreadPoolExecutor dedicato per handshake comporta la creazione/distruzione
    di un thread OS ad ogni singolo handshake — overhead evitabile sul percorso
    più frequente del provider arqit_py.
    """
    if inspect.isawaitable(result):
        return await result
    if not (hasattr(result, "get") and callable(result.get)):
        return result
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, result.get)