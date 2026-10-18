"""
stls_core_providers_pqc.py
--------------------------
Provider PQC (Post-Quantum Cryptography) per STLS.

Implementa key agreement ibrido X25519 + ML-KEM-768 (NIST FIPS 203):
  - X25519 (ECDH su Curve25519): via pyca/cryptography, già dipendenza
    di stls-core. Sicuro contro computer classici.
  - ML-KEM-768 (ex Kyber768, NIST FIPS 203): via liboqs-python,
    dipendenza opzionale [pqc]. Sicuro contro computer quantistici
    (lattice-based — resistente a Shor's algorithm).

La combinazione ibrida garantisce che rompere entrambi gli algoritmi
contemporaneamente sia necessario: se X25519 cedesse a un computer
quantistico, ML-KEM-768 protegge. Se ML-KEM-768 avesse una vulnerabilità
ignota, X25519 protegge.

Selezione: KEY_PROVIDER_NAME=pqc
Dipendenza opzionale: pip install 'stls-core[pqc]' (installa liboqs-python)

A differenza di arqit_py, non esiste un servizio di peering esterno:
la chiave è derivata localmente durante ogni handshake tramite KEM.
La stessa SessionCache (memory o redis) degli altri provider conserva
la session_key derivata associata al session_id.

Quantum safety — nota sull'entropia:
  La quantum-safety viene dall'algoritmo lattice-based (ML-KEM), non
  dalla "qualità quantistica" del random. os.urandom() via CSPRNG del
  kernel (ChaCha20 su Linux) fornisce 256 bit di entropia — sufficiente
  per sicurezza NIST Level 3 (ML-KEM-768). QRNG hardware è un futuro
  miglioramento iniettabile via HKDF-Extract come salt esterno.

Struttura:
  PQCKeyStore               — dict thread-safe per le chiavi derivate
  KeySessionProviderPQC     — implementa KeySessionProvider
  KeyProviderClientPQC      — implementa KeyProviderClient
  KeyProviderServerPQC      — implementa KeyProviderServer
  KeyProviderPQC            — implementa KeyProvider (factory)
  mlkem_generate_keypair()  — genera keypair ML-KEM-768
  mlkem_encap()             — KEM encapsulation (server con pubkey client)
  mlkem_decap()             — KEM decapsulation (client con ciphertext server)
  x25519_generate_keypair() — genera keypair X25519
  x25519_exchange()         — calcola X25519 shared secret
  derive_hybrid_key()       — HKDF dai due shared secret
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from .stls_core_providers_base import (
    BlockableResult,
    KeyProvider,
    KeyProviderClient,
    KeyProviderServer,
    KeySessionProvider,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Algoritmo ML-KEM-768 — nomi supportati da liboqs
# ---------------------------------------------------------------------------

_MLKEM_CANDIDATES = ("ML-KEM-768", "Kyber768")
_MLKEM_ALG_CACHE: Optional[str] = None


# ---------------------------------------------------------------------------
# PQCKeyStore — store in-memory per le chiavi derivate via KEM
# ---------------------------------------------------------------------------

class PQCKeyStore:
    """
    Store thread-safe in-memory per le chiavi di sessione derivate via PQC.

    Diversamente da ArqitKeyStoreBase (che gestisce chiavi asincrone da
    un peering esterno), PQCKeyStore è un semplice dict perché le chiavi
    PQC sono derivate localmente e sincrono durante l'handshake.

    Il TTL è gestito dalla SessionCache a livello superiore — questa
    classe mantiene solo la correlazione kid → bytes per il lookup in
    getSessionKeyData().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, bytes] = {}

    def put(self, kid: str, key_bytes: bytes) -> None:
        """Salva una chiave derivata tramite KEM."""
        with self._lock:
            self._keys[kid] = key_bytes
        logger.debug("[PQCKeyStore] chiave salvata: kid=%s len=%d", kid, len(key_bytes))

    def get(self, kid: str) -> Optional[bytes]:
        """Recupera la chiave associata al kid, o None."""
        with self._lock:
            return self._keys.get(kid)

    def remove(self, kid: str) -> bool:
        """Rimuove la chiave. Restituisce True se esisteva."""
        with self._lock:
            return bool(self._keys.pop(kid, None))

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)


# ---------------------------------------------------------------------------
# KeySessionProviderPQC
# ---------------------------------------------------------------------------

class KeySessionProviderPQC(KeySessionProvider):
    """
    KeySessionProvider per il provider PQC.

    SERVER: la chiave viene iniettata via inject_key() da
    perform_pqc_handshake() dopo il KEM exchange. getSessionKeyIdentifier()
    e getSessionKeyData() la espongono alla pipeline standard.

    CLIENT: tiene i keypair X25519 e ML-KEM generati da
    KeyProviderClientPQC.start(), esposti come property per il KEM
    lato client (decap + DH) dopo la PQCHandshakeResponse.
    """

    def __init__(
        self,
        session_type: KeySessionProvider.KeySessionProviderType,
        key_store: PQCKeyStore,
        x25519_private_key_bytes: Optional[bytes] = None,
        mlkem_secret_key_bytes: Optional[bytes] = None,
    ) -> None:
        self._session_type = session_type
        self._key_store = key_store
        self._kid: Optional[str] = None
        # Client only — keypair per KEM lato client
        self._x25519_private_key_bytes = x25519_private_key_bytes
        self._mlkem_secret_key_bytes = mlkem_secret_key_bytes

    def getSessionType(self) -> KeySessionProvider.KeySessionProviderType:
        return self._session_type

    async def getSessionKeyIdentifier(self) -> str:
        """
        Restituisce il kid della sessione corrente.

        SERVER: il kid viene iniettato da inject_key() da
        perform_pqc_handshake() prima che questa venga chiamata.
        CLIENT: il kid è impostato via setSessionKeyIdentifier()
        dopo la ricezione della PQCHandshakeResponse.
        """
        if self._kid is None:
            raise RuntimeError(
                "[KeySessionProviderPQC] kid non disponibile — "
                "inject_key() o setSessionKeyIdentifier() non ancora chiamati"
            )
        return self._kid

    def setSessionKeyIdentifier(self, kid: str) -> None:
        self._kid = kid
        logger.debug("[KeySessionProviderPQC] kid impostato: %s", kid)

    async def getSessionKeyData(self, session_key_identifier: str) -> bytes:
        """Restituisce la chiave derivata tramite KEM per il kid dato."""
        key = self._key_store.get(session_key_identifier)
        if key is None:
            raise KeyError(
                f"[KeySessionProviderPQC] chiave non trovata: "
                f"kid={session_key_identifier!r}"
            )
        return key

    def inject_key(self, kid: str, session_key: bytes) -> None:
        """
        Inietta la chiave derivata da perform_pqc_handshake() nel key store.

        Chiamato dal server dopo ogni KEM exchange per rendere la chiave
        disponibile a getSessionKeyData() e aggiornare il kid corrente.
        Ogni handshake PQC produce un kid distinto (UUID), quindi questa
        istanza può servire handshake successivi senza rischio di riuso.
        """
        self._key_store.put(kid, session_key)
        self._kid = kid
        logger.debug(
            "[KeySessionProviderPQC] chiave iniettata: kid=%s len=%d",
            kid, len(session_key),
        )

    def getPeerIdentity(self) -> Optional[str]:
        return None

    # ── Proprietà client-only ─────────────────────────────────────────────────

    @property
    def x25519_private_key_bytes(self) -> Optional[bytes]:
        """Chiave privata X25519 del client — usata in x25519_exchange()."""
        return self._x25519_private_key_bytes

    @property
    def mlkem_secret_key_bytes(self) -> Optional[bytes]:
        """Secret key ML-KEM-768 del client — usata in mlkem_decap()."""
        return self._mlkem_secret_key_bytes


# ---------------------------------------------------------------------------
# KeyProviderClientPQC
# ---------------------------------------------------------------------------

class KeyProviderClientPQC(KeyProviderClient):
    """
    KeyProviderClient PQC — lato client.

    start(): genera keypair X25519 e ML-KEM-768 per questa sessione.
    Le pubkey vengono incluse nella PQCHandshakeRequest; le chiavi
    private rimangono locali per il decap + DH dopo la risposta.

    createProviderSession(): espone i keypair tramite KeySessionProviderPQC
    in modo che il codice client possa derivare la session_key.
    """

    def __init__(self) -> None:
        self._key_store = PQCKeyStore()
        self._session: Optional[KeySessionProviderPQC] = None
        self._x25519_public_key_bytes: Optional[bytes] = None
        self._mlkem_public_key_bytes: Optional[bytes] = None

    def start(self) -> None:
        """Genera keypair X25519 + ML-KEM-768 per questa sessione."""
        x25519_priv, x25519_pub = x25519_generate_keypair()
        mlkem_pub, mlkem_sec = mlkem_generate_keypair()

        self._x25519_public_key_bytes = x25519_pub
        self._mlkem_public_key_bytes = mlkem_pub

        self._session = KeySessionProviderPQC(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            key_store=self._key_store,
            x25519_private_key_bytes=x25519_priv,
            mlkem_secret_key_bytes=mlkem_sec,
        )
        logger.info("[KeyProviderClientPQC] keypair X25519 + ML-KEM-768 generati")

    def createProviderSession(self) -> KeySessionProvider:
        if self._session is None:
            raise RuntimeError("[KeyProviderClientPQC] start() non ancora chiamato")
        return self._session

    def stop(self) -> None:
        """Azzera le chiavi private in memoria (best-effort in Python)."""
        if self._session is not None:
            if self._session._x25519_private_key_bytes:
                self._session._x25519_private_key_bytes = (
                    b"\x00" * len(self._session._x25519_private_key_bytes)
                )
            if self._session._mlkem_secret_key_bytes:
                self._session._mlkem_secret_key_bytes = (
                    b"\x00" * len(self._session._mlkem_secret_key_bytes)
                )
        logger.debug("[KeyProviderClientPQC] stop() — chiavi private azzerate")

    @property
    def x25519_public_key_bytes(self) -> bytes:
        """Pubkey X25519 da includere nella PQCHandshakeRequest."""
        if self._x25519_public_key_bytes is None:
            raise RuntimeError("[KeyProviderClientPQC] start() non ancora chiamato")
        return self._x25519_public_key_bytes

    @property
    def mlkem_public_key_bytes(self) -> bytes:
        """Pubkey ML-KEM-768 da includere nella PQCHandshakeRequest."""
        if self._mlkem_public_key_bytes is None:
            raise RuntimeError("[KeyProviderClientPQC] start() non ancora chiamato")
        return self._mlkem_public_key_bytes


# ---------------------------------------------------------------------------
# KeyProviderServerPQC
# ---------------------------------------------------------------------------

class KeyProviderServerPQC(KeyProviderServer):
    """
    KeyProviderServer PQC — lato server.

    Non avvia thread di peering: la chiave è derivata per ogni handshake
    da perform_pqc_handshake() che chiama inject_key() sulla stessa istanza
    KeySessionProviderPQC. start() è un no-op (nessun servizio esterno).

    La stessa istanza serve tutti gli handshake nel corso della vita del
    processo, coerente con il pattern di arqit_py. Ogni handshake produce
    un kid distinto (UUID) — nessun riuso di chiavi tra sessioni diverse.
    """

    def __init__(self) -> None:
        self._key_store = PQCKeyStore()
        self._session: Optional[KeySessionProviderPQC] = None

    async def start(self) -> BlockableResult:
        """Inizializza la sessione provider. Nessun thread di peering."""
        self._session = KeySessionProviderPQC(
            session_type=KeySessionProvider.KeySessionProviderType.SERVER,
            key_store=self._key_store,
        )
        logger.info(
            "[KeyProviderServerPQC] avviato — "
            "key agreement KEM per-handshake (nessun peering esterno)"
        )
        return BlockableResult()

    def createProviderSession(self) -> KeySessionProvider:
        if self._session is None:
            raise RuntimeError("[KeyProviderServerPQC] start() non ancora chiamato")
        return self._session

    async def stop(self) -> None:
        logger.info("[KeyProviderServerPQC] stop()")


# ---------------------------------------------------------------------------
# KeyProviderPQC — factory
# ---------------------------------------------------------------------------

class KeyProviderPQC(KeyProvider):
    """
    KeyProvider PQC — ibrido X25519 + ML-KEM-768.

    Selezione: KEY_PROVIDER_NAME=pqc
    Richiede liboqs-python installato via [pqc] extra.

    L'handshake PQC usa un endpoint dedicato (/stls/pqc/handshake) con
    wire format esteso (PQCHandshakeRequest / PQCHandshakeResponse) rispetto
    all'handshake standard. I layer stls-a2a e stls-mcp registrano
    entrambi gli endpoint quando KEY_PROVIDER_NAME=pqc.
    """

    def get_name(self) -> str:
        return "pqc"

    def create_provider_client(
        self, host: str, port: int, options: dict[str, Any]
    ) -> KeyProviderClient:
        _check_liboqs()
        return KeyProviderClientPQC()

    def create_provider_server(
        self, port: int, options: dict[str, Any]
    ) -> KeyProviderServer:
        _check_liboqs()
        return KeyProviderServerPQC()


# ---------------------------------------------------------------------------
# Funzioni crittografiche PQC — public API
# ---------------------------------------------------------------------------

def _check_liboqs() -> None:
    """
    Verifica che liboqs-python sia installato E compatibile con la C library.

    Un mismatch di versione tra liboqs-python (wrapper) e liboqs C library
    (sistema) causa AttributeError a import time — non ImportError.
    Esempio: liboqs-python 0.15.0 + liboqs C 0.11.0 → AttributeError su
    OQS_SIG_supports_ctx_str. Entrambi i casi vengono normalizzati in
    ImportError con istruzioni chiare.
    """
    try:
        import oqs  # noqa: F401
    except ImportError:
        raise ImportError(
            "Il provider 'pqc' richiede liboqs-python.\n"
            "Installazione:\n"
            "  uv pip install -e '.[pqc]'\n"
            "  oppure: pip install liboqs-python==0.11.0"
        ) from None
    except (AttributeError, OSError) as exc:
        raise ImportError(
            f"liboqs-python è installato ma incompatibile con la C library di sistema: {exc}\n"
            "La versione del wrapper Python deve corrispondere alla liboqs C library.\n"
            "Fix:\n"
            "  uv pip install liboqs-python==0.11.0\n"
            "  (sostituire 0.11.0 con la versione della C library installata)"
        ) from exc


def _mlkem_alg_name() -> str:
    """
    Restituisce il nome ML-KEM-768 supportato da questa versione di liboqs.

    Tenta "ML-KEM-768" (liboqs >= 0.10, post-standardizzazione NIST)
    poi "Kyber768" (liboqs < 0.10, nome pre-standard).
    Il risultato è memoizzato per evitare probe ripetuti.
    """
    global _MLKEM_ALG_CACHE
    if _MLKEM_ALG_CACHE is not None:
        return _MLKEM_ALG_CACHE

    import oqs
    for candidate in _MLKEM_CANDIDATES:
        try:
            kem = oqs.KeyEncapsulation(candidate)
            kem.free()
            _MLKEM_ALG_CACHE = candidate
            logger.debug("[PQC] algoritmo ML-KEM selezionato: %s", candidate)
            return candidate
        except Exception:
            pass

    raise RuntimeError(
        f"Nessun algoritmo ML-KEM-768 disponibile in liboqs. "
        f"Tentati: {list(_MLKEM_CANDIDATES)}. "
        f"Verifica l'installazione di liboqs-python."
    )


def mlkem_generate_keypair() -> tuple[bytes, bytes]:
    """
    Genera keypair ML-KEM-768.

    Usata dal client per generare le chiavi da includere nella
    PQCHandshakeRequest.

    Returns:
        (public_key_bytes, secret_key_bytes) — public key da inviare al server,
        secret key da conservare localmente per la decapsulation.
    """
    import oqs
    alg = _mlkem_alg_name()
    kem = oqs.KeyEncapsulation(alg)
    try:
        public_key = kem.generate_keypair()
        secret_key = kem.export_secret_key()
    finally:
        kem.free()

    logger.debug(
        "[PQC] ML-KEM keypair: alg=%s pk_len=%d sk_len=%d",
        alg, len(public_key), len(secret_key),
    )
    return public_key, secret_key


def mlkem_encap(client_mlkem_public_key: bytes) -> tuple[bytes, bytes]:
    """
    ML-KEM encapsulation — eseguita dal SERVER con la pubkey del client.

    Produce un ciphertext (da inviare al client) e uno shared secret
    (usato localmente per derivare la session_key). Solo chi possiede
    la corrispondente secret key ML-KEM può ricavare lo stesso secret
    dal ciphertext.

    Args:
        client_mlkem_public_key: Pubkey ML-KEM-768 del client (bytes).

    Returns:
        (ciphertext_bytes, shared_secret_bytes).
        ciphertext → PQCHandshakeResponse.mlkem_ciphertext.
        shared_secret → input per derive_hybrid_key().
    """
    import oqs
    alg = _mlkem_alg_name()
    kem = oqs.KeyEncapsulation(alg)
    try:
        ciphertext, shared_secret = kem.encap_secret(client_mlkem_public_key)
    finally:
        kem.free()

    logger.debug(
        "[PQC] ML-KEM encap: ct_len=%d secret_len=%d",
        len(ciphertext), len(shared_secret),
    )
    return ciphertext, shared_secret


def mlkem_decap(
    client_mlkem_secret_key: bytes,
    ciphertext: bytes,
) -> bytes:
    """
    ML-KEM decapsulation — eseguita dal CLIENT con il ciphertext del server.

    Recupera lo stesso shared secret prodotto dal server durante
    mlkem_encap(). Il risultato coincide con shared_secret del server
    solo se il ciphertext è stato prodotto con la pubkey corrispondente
    a questa secret key.

    Args:
        client_mlkem_secret_key: Secret key ML-KEM-768 del client (bytes).
        ciphertext:              Ciphertext ricevuto nella PQCHandshakeResponse.

    Returns:
        shared_secret (bytes) — stesso valore del server.
    """
    import oqs
    alg = _mlkem_alg_name()
    kem = oqs.KeyEncapsulation(alg, secret_key=client_mlkem_secret_key)
    try:
        shared_secret = kem.decap_secret(ciphertext)
    finally:
        kem.free()

    logger.debug("[PQC] ML-KEM decap: secret_len=%d", len(shared_secret))
    return shared_secret


def x25519_generate_keypair() -> tuple[bytes, bytes]:
    """
    Genera keypair X25519.

    Returns:
        (private_key_bytes, public_key_bytes) — entrambi 32 byte.
        private_key → usata in x25519_exchange().
        public_key → inviata nella PQCHandshakeRequest / PQCHandshakeResponse.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption,
    )

    priv = X25519PrivateKey.generate()
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def x25519_exchange(
    private_key_bytes: bytes,
    peer_public_key_bytes: bytes,
) -> bytes:
    """
    Calcola lo shared secret X25519 (Diffie-Hellman su Curve25519).

    Eseguita da entrambi i lati con chiave privata locale e pubkey del peer:
    il risultato è lo stesso per client e server.

    Args:
        private_key_bytes:      32 byte chiave privata X25519.
        peer_public_key_bytes:  32 byte chiave pubblica X25519 del peer.

    Returns:
        32 byte shared secret.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )

    priv = X25519PrivateKey.from_private_bytes(private_key_bytes)
    pub = X25519PublicKey.from_public_bytes(peer_public_key_bytes)
    shared = priv.exchange(pub)
    logger.debug("[PQC] X25519 exchange: shared_len=%d", len(shared))
    return shared


def derive_hybrid_key(
    x25519_secret: bytes,
    mlkem_secret: bytes,
    nonce: bytes,
    info: bytes = b"stls-pqc-hybrid-v1",
    length: int = 32,
) -> bytes:
    """
    Deriva la session_key finale dai due shared secret via HKDF-SHA256.

    Schema (RFC 5869 completo — Extract + Expand):
        IKM  = x25519_secret ‖ mlkem_secret   (64 byte)
        salt = nonce                           (32 byte random per-sessione)
        info = b"stls-pqc-hybrid-v1"          (domain separator)
        len  = 32 byte

    Proprietà di sicurezza:
      - Il nonce come salt garantisce freshness per ogni sessione.
      - La concatenazione IKM è sufficiente per il modello ibrido:
        rompere entrambi gli algoritmi contemporaneamente è necessario.
      - L'info label separa questa chiave da quelle AES/HMAC derivate
        successivamente da StlsTransport (via HKDFExpand).

    Args:
        x25519_secret: 32 byte shared secret X25519.
        mlkem_secret:  Shared secret ML-KEM-768 (tipicamente 32 byte).
        nonce:         Nonce della PQCHandshakeRequest (≥ 16 byte).
        info:          Domain separator (default b"stls-pqc-hybrid-v1").
        length:        Byte della chiave derivata (default 32 = AES-256).

    Returns:
        `length` byte session_key da passare a StlsTransport.
    """
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    ikm = x25519_secret + mlkem_secret

    key = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=nonce,
        info=info,
    ).derive(ikm)

    logger.debug(
        "[PQC] HKDF ibrido: ikm_len=%d salt_len=%d → key_len=%d",
        len(ikm), len(nonce), len(key),
    )
    return key