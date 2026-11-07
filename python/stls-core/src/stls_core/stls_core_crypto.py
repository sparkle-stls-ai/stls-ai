"""
stls_core_crypto.py
-------------------
Protocol-agnostic cryptographic layer for STLS.

Implements:
  - AES-256-GCM for ciphertext confidentiality and authentication
  - An additional HMAC-SHA256 over the full envelope (IV + ciphertext + GCM tag)
  - HKDF-Expand (RFC 5869 §3.2) to derive distinct AES and HMAC keys
    from the session key, using a domain separator as the info label

Dependencies: cryptography>=46.0.5

This module knows nothing about A2A, MCP, FastAPI, httpx or any
transport protocol. It works exclusively on bytes and JSON-serializable
dicts.

Typical usage:
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
# StlsEnvelope — encrypted envelope structure
# ---------------------------------------------------------------------------

class StlsEnvelope:
    """
    Typed representation of the encrypted STLS envelope.

    Can be serialized into a JSON dict for HTTP transport and
    reconstructed from the received dict.

    Wire structure:
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
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, str]:
        """Serializes into a JSON-serializable dict (base64 for the bytes fields)."""
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
        """Reconstructs an envelope from the dict received over HTTP."""
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
# StlsTransport — encrypt / decrypt over plain bytes
# ---------------------------------------------------------------------------

class StlsTransport:
    """
    STLS transport: encrypts and decrypts arbitrary payloads (bytes).

    Protocol-agnostic: knows nothing about A2A, MCP or other protocols.
    The layers above (stls-a2a, stls-mcp) are responsible for serializing
    their own message format into bytes before calling encrypt(),
    and deserializing it after decrypt().

    Lifecycle:
        1. The server generates the session_key via KeyProvider and shares
           it with the client through the STLS handshake (/stls/handshake).
        2. Client and server each instantiate a StlsTransport with the
           same session_key and session_id.
        3. Every payload is encrypted with encrypt() and decrypted with decrypt().
        4. The AES and HMAC keys are derived via HKDF-Expand from the
           session_key with distinct domain separators — they are never
           used directly.

    Thread-safety: each instance is stateless after __init__; encrypt/decrypt
    do not mutate internal state and are safe for concurrent calls.
    """

    def __init__(self, session_key: bytes, session_id: str) -> None:
        """
        Initializes the transport.

        Args:
            session_key: Shared session key (output of the KeyProvider).
                         Minimum 16 bytes; typically 32 bytes (256 bit).
            session_id:  Unique identifier of the STLS session (UUID).
        """
        self.session_id = session_id

        # Derives the AES and HMAC keys via HKDF-Expand.
        # The SKA session_key is already high-entropy → only Expand is used
        # (no Extract phase). The two info labels act as domain separators:
        # the two derived keys never share cryptographic material.
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
    # Public API
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: bytes) -> StlsEnvelope:
        """
        Encrypts an arbitrary payload.

        Pipeline:
            1. Generates a random 96-bit IV (12 bytes).
            2. Encrypts with AES-256-GCM → ciphertext + GCM tag (128-bit).
            3. Computes HMAC-SHA256 over (IV ‖ ciphertext ‖ GCM tag).
            4. Returns a StlsEnvelope with all components.

        Args:
            plaintext: Payload to encrypt (any bytes).

        Returns:
            StlsEnvelope ready for serialization and transport.
        """
        iv = os.urandom(12)  # 96-bit for AES-GCM

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
        Decrypts a STLS envelope.

        Pipeline:
            1. Verifies session_id.
            2. Verifies HMAC-SHA256 over the envelope (IV ‖ ciphertext ‖ tag).
            3. Decrypts with AES-256-GCM (verifies the GCM tag internally).
            4. Returns the original plaintext.

        Args:
            envelope: StlsEnvelope received from the peer.

        Returns:
            Original plaintext as bytes.

        Raises:
            ValueError: If session_id doesn't match or the HMAC / GCM
                        verification fails (integrity compromised).
        """
        if envelope.session_id != self.session_id:
            raise ValueError(
                f"Session ID mismatch: expected {self.session_id!r}, "
                f"got {envelope.session_id!r}"
            )

        # Verifies the HMAC before attempting decryption (fail-fast)
        expected_mac = self._compute_mac(
            envelope.iv, envelope.ciphertext, envelope.tag
        )
        if not self._constant_time_equal(expected_mac, envelope.mac):
            raise ValueError(
                "MAC verification failed — message integrity compromised"
            )

        # AES-256-GCM decryption (verifies the GCM tag internally)
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
    # JSON helpers — convenience for the layers above
    # ------------------------------------------------------------------

    def encrypt_json(self, payload: Any) -> dict[str, str]:
        """
        Serializes a payload to JSON and encrypts it, returns a wire-ready dict.

        Shortcut used by stls-a2a and stls-mcp to encrypt a dict/object
        without manually handling serialization.

        Args:
            payload: Any JSON-serializable object.

        Returns:
            Dict (StlsEnvelope.to_dict()) ready to be sent over HTTP.
        """
        plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.encrypt(plaintext).to_dict()

    def decrypt_json(self, envelope_dict: dict[str, str]) -> Any:
        """
        Reconstructs the envelope from the received dict, decrypts and deserializes JSON.

        Args:
            envelope_dict: Dict received over HTTP (output of encrypt_json).

        Returns:
            Python object deserialized from the original JSON.

        Raises:
            ValueError: If the cryptographic verification fails.
            json.JSONDecodeError: If the plaintext is not valid JSON.
        """
        envelope = StlsEnvelope.from_dict(envelope_dict)
        plaintext = self.decrypt(envelope)
        return json.loads(plaintext.decode("utf-8"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_mac(self, iv: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        """Computes HMAC-SHA256 over (IV ‖ ciphertext ‖ GCM tag)."""
        h = hmac.HMAC(self._hmac_key, hashes.SHA256())
        h.update(iv + ciphertext + tag)
        return h.finalize()

    @staticmethod
    def _constant_time_equal(a: bytes, b: bytes) -> bool:
        """Timing-safe comparison between two byte strings."""
        return _hmac.compare_digest(a, b)
