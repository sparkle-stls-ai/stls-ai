"""
stls_core_crypto.py
-------------------
Layer crittografico protocol-agnostic per STLS.

Implementa:
  - AES-256-GCM per confidenzialità e autenticazione del ciphertext
  - HMAC-SHA256 aggiuntivo sull'envelope completo (IV + ciphertext + GCM tag)
  - HKDF-Expand (RFC 5869 §3.2) per derivare chiavi AES e HMAC distinte
    dalla session key, usando domain separator come info label

Dipendenze: cryptography>=46.0.5

Questo modulo non conosce A2A, MCP, FastAPI, httpx o qualsiasi protocollo
di trasporto. Lavora esclusivamente su bytes e dict JSON-serializzabili.

Uso tipico:
    transport = StlsTransport(session_key=key, session_id=sid)
    envelope  = transport.encrypt(plaintext_bytes)
    recovered = transport.decrypt(envelope)
"""

import base64
import hmac as _hmac
import json
import logging
import os
from typing import Any

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from .stls_core_constants import (
    ENVELOPE_CIPHERTEXT,
    ENVELOPE_IV,
    ENVELOPE_MAC,
    ENVELOPE_SESSION_ID,
    ENVELOPE_TAG,
    ENVELOPE_VERSION,
    HKDF_INFO_AES,
    HKDF_INFO_HMAC,
    STLS_VERSION,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StlsEnvelope — struttura dell'envelope cifrato
# ---------------------------------------------------------------------------

class StlsEnvelope:
    """
    Rappresentazione tipizzata dell'envelope STLS cifrato.

    Può essere serializzata in dict JSON per il trasporto HTTP
    e ricostruita dal dict ricevuto.

    Struttura wire:
        {
            "version":    "1.0",
            "session_id": "<uuid>",
            "iv":         "<base64 96-bit>",
            "ciphertext": "<base64>",
            "tag":        "<base64 128-bit GCM tag>",
            "mac":        "<base64 SHA-256 HMAC>"
        }
    """

    __slots__ = ("version", "session_id", "iv", "ciphertext", "tag", "mac")

    def __init__(
        self,
        session_id: str,
        iv: bytes,
        ciphertext: bytes,
        tag: bytes,
        mac: bytes,
        version: str = STLS_VERSION,
    ) -> None:
        self.version = version
        self.session_id = session_id
        self.iv = iv
        self.ciphertext = ciphertext
        self.tag = tag
        self.mac = mac

    # ------------------------------------------------------------------
    # Serializzazione
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, str]:
        """Serializza in dict JSON-serializzabile (base64 per i bytes)."""
        return {
            ENVELOPE_VERSION:    self.version,
            ENVELOPE_SESSION_ID: self.session_id,
            ENVELOPE_IV:         base64.b64encode(self.iv).decode("utf-8"),
            ENVELOPE_CIPHERTEXT: base64.b64encode(self.ciphertext).decode("utf-8"),
            ENVELOPE_TAG:        base64.b64encode(self.tag).decode("utf-8"),
            ENVELOPE_MAC:        base64.b64encode(self.mac).decode("utf-8"),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "StlsEnvelope":
        """Ricostruisce un envelope dal dict ricevuto via HTTP."""
        return cls(
            version=data.get(ENVELOPE_VERSION, STLS_VERSION),
            session_id=data[ENVELOPE_SESSION_ID],
            iv=base64.b64decode(data[ENVELOPE_IV]),
            ciphertext=base64.b64decode(data[ENVELOPE_CIPHERTEXT]),
            tag=base64.b64decode(data[ENVELOPE_TAG]),
            mac=base64.b64decode(data[ENVELOPE_MAC]),
        )

    def __repr__(self) -> str:
        return (
            f"StlsEnvelope(session_id={self.session_id!r}, "
            f"iv_len={len(self.iv)}, ct_len={len(self.ciphertext)})"
        )


# ---------------------------------------------------------------------------
# StlsTransport — encrypt / decrypt su bytes puri
# ---------------------------------------------------------------------------

class StlsTransport:
    """
    Trasporto STLS: cifra e decifra payload arbitrari (bytes).

    Protocol-agnostic: non conosce A2A, MCP o altri protocolli.
    I layer superiori (stls-a2a, stls-mcp) si occupano di serializzare
    il proprio formato messaggio in bytes prima di chiamare encrypt(),
    e di deserializzarlo dopo decrypt().

    Ciclo di vita:
        1. Il server genera session_key via KeyProvider e la condivide
           col client tramite handshake STLS (/stls/handshake).
        2. Client e server istanziano ciascuno un StlsTransport con la
           stessa session_key e session_id.
        3. Ogni payload viene cifrato con encrypt() e decifrato con decrypt().
        4. Le chiavi AES e HMAC sono derivate via HKDF-Expand dalla
           session_key con domain separator distinti — non vengono mai
           usate direttamente.

    Thread-safety: ogni istanza è stateless dopo __init__; encrypt/decrypt
    non modificano lo stato interno e sono safe per chiamate concorrenti.
    """

    def __init__(self, session_key: bytes, session_id: str) -> None:
        """
        Inizializza il trasporto.

        Args:
            session_key: Chiave di sessione condivisa (output del KeyProvider).
                         Minimo 16 byte; tipicamente 32 byte (256 bit).
            session_id:  Identificatore univoco della sessione STLS (UUID).
        """
        self.session_id = session_id

        # Deriva chiavi AES e HMAC via HKDF-Expand.
        # La session_key SKA è già ad alta entropia → si usa solo Expand
        # (nessuna fase Extract). I due info label fungono da domain separator:
        # le due chiavi derivate non condividono mai materiale crittografico.
        self._aes_key: bytes = HKDFExpand(
            algorithm=hashes.SHA256(),
            length=32,
            info=HKDF_INFO_AES,
        ).derive(session_key)

        self._hmac_key: bytes = HKDFExpand(
            algorithm=hashes.SHA256(),
            length=32,
            info=HKDF_INFO_HMAC,
        ).derive(session_key)

        logger.debug(
            "StlsTransport inizializzato: session_id=%s", session_id
        )

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: bytes) -> StlsEnvelope:
        """
        Cifra un payload arbitrario.

        Pipeline:
            1. Genera IV random 96-bit (12 byte).
            2. Cifra con AES-256-GCM → ciphertext + GCM tag (128-bit).
            3. Calcola HMAC-SHA256 su (IV ‖ ciphertext ‖ GCM tag).
            4. Restituisce StlsEnvelope con tutti i componenti.

        Args:
            plaintext: Payload da cifrare (qualsiasi bytes).

        Returns:
            StlsEnvelope pronto per la serializzazione e il trasporto.
        """
        iv = os.urandom(12)  # 96-bit per AES-GCM

        cipher = Cipher(
            algorithms.AES(self._aes_key),
            modes.GCM(iv),
        )
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()
        tag = encryptor.tag  # 128-bit GCM authentication tag

        mac = self._compute_mac(iv, ciphertext, tag)

        logger.debug(
            "encrypt: session_id=%s ct_len=%d",
            self.session_id, len(ciphertext),
        )

        return StlsEnvelope(
            session_id=self.session_id,
            iv=iv,
            ciphertext=ciphertext,
            tag=tag,
            mac=mac,
        )

    def decrypt(self, envelope: StlsEnvelope) -> bytes:
        """
        Decifra un envelope STLS.

        Pipeline:
            1. Verifica session_id.
            2. Verifica HMAC-SHA256 sull'envelope (IV ‖ ciphertext ‖ tag).
            3. Decifra con AES-256-GCM (verifica il GCM tag internamente).
            4. Restituisce il plaintext originale.

        Args:
            envelope: StlsEnvelope ricevuto dal peer.

        Returns:
            Plaintext originale come bytes.

        Raises:
            ValueError: Se session_id non corrisponde o la verifica
                        HMAC / GCM fallisce (integrità compromessa).
        """
        if envelope.session_id != self.session_id:
            raise ValueError(
                f"Session ID mismatch: atteso {self.session_id!r}, "
                f"ricevuto {envelope.session_id!r}"
            )

        # Verifica HMAC prima di tentare la decifratura (fail-fast)
        expected_mac = self._compute_mac(
            envelope.iv, envelope.ciphertext, envelope.tag
        )
        if not self._constant_time_equal(expected_mac, envelope.mac):
            raise ValueError(
                "MAC verification failed — integrità del messaggio compromessa"
            )

        # Decifratura AES-256-GCM (verifica GCM tag internamente)
        cipher = Cipher(
            algorithms.AES(self._aes_key),
            modes.GCM(envelope.iv, envelope.tag),
        )
        decryptor = cipher.decryptor()
        try:
            plaintext = decryptor.update(envelope.ciphertext) + decryptor.finalize()
        except Exception as exc:
            raise ValueError(
                f"AES-GCM decryption failed — tag non valido o dati corrotti: {exc}"
            ) from exc

        logger.debug(
            "decrypt: session_id=%s pt_len=%d",
            self.session_id, len(plaintext),
        )

        return plaintext

    # ------------------------------------------------------------------
    # Helpers JSON — convenienza per i layer superiori
    # ------------------------------------------------------------------

    def encrypt_json(self, payload: Any) -> dict[str, str]:
        """
        Serializza payload in JSON e cifra, restituisce dict wire-ready.

        Shortcut usato da stls-a2a e stls-mcp per cifrare un dict/oggetto
        senza gestire manualmente la serializzazione.

        Args:
            payload: Qualsiasi oggetto JSON-serializzabile.

        Returns:
            Dict (StlsEnvelope.to_dict()) pronto per essere inviato via HTTP.
        """
        plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.encrypt(plaintext).to_dict()

    def decrypt_json(self, envelope_dict: dict[str, str]) -> Any:
        """
        Ricostruisce l'envelope dal dict ricevuto, decifra e deserializza JSON.

        Args:
            envelope_dict: Dict ricevuto via HTTP (output di encrypt_json).

        Returns:
            Oggetto Python deserializzato dal JSON originale.

        Raises:
            ValueError: Se la verifica crittografica fallisce.
            json.JSONDecodeError: Se il plaintext non è JSON valido.
        """
        envelope = StlsEnvelope.from_dict(envelope_dict)
        plaintext = self.decrypt(envelope)
        return json.loads(plaintext.decode("utf-8"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_mac(self, iv: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        """Calcola HMAC-SHA256 su (IV ‖ ciphertext ‖ GCM tag)."""
        h = hmac.HMAC(self._hmac_key, hashes.SHA256())
        h.update(iv + ciphertext + tag)
        return h.finalize()

    @staticmethod
    def _constant_time_equal(a: bytes, b: bytes) -> bool:
        """Confronto timing-safe tra due byte string."""
        return _hmac.compare_digest(a, b)