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
from typing import Any, Literal, Optional

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
    HKDF_INFO_AES_C2S,
    HKDF_INFO_AES_S2C,
    HKDF_INFO_HMAC,
    HKDF_INFO_HMAC_C2S,
    HKDF_INFO_HMAC_S2C,
    STLS_VERSION,
)

StlsDirection = Literal["c2s", "s2c"]

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

    Direction (`direction` param, optional):
        By default (direction=None) both peers derive a single AES/HMAC
        key pair and use it for both encrypt() and decrypt() — this is
        the mode used by stls-a2a and stls-mcp today, where the same
        transport object serves both directions of one session.

        Passing direction="c2s" or "s2c" instead derives four keys
        (AES+HMAC for each direction) and splits them so that the two
        directions of a session never share key material: a peer
        declaring "c2s" encrypts with the c2s keys and decrypts with the
        s2c keys, and a peer declaring "s2c" does the opposite. Both
        peers must agree on who is "c2s" and who is "s2c" out of band
        (there is no handshake negotiation of this). Intended for
        callers that label the two ends of a session themselves (e.g.
        two peer nodes), not for the existing A2A/MCP request/response
        flows, which don't need it.

    Thread-safety: each instance is stateless after __init__; encrypt/decrypt
    do not mutate internal state and are safe for concurrent calls.
    """

    def __init__(
        self,
        session_key: bytes,
        session_id: str,
        direction: Optional[StlsDirection] = None,
    ) -> None:
        """
        Initializes the transport.

        Args:
            session_key: Shared session key (output of the KeyProvider).
                         Minimum 16 bytes; typically 32 bytes (256 bit).
            session_id:  Unique identifier of the STLS session (UUID).
            direction:   None (default) for a single shared key pair used
                         for both directions. "c2s" or "s2c" to derive
                         independent per-direction keys instead — see the
                         class docstring.
        """
        if direction is not None and direction not in ("c2s", "s2c"):
            raise ValueError(f"Invalid direction: {direction!r} (expected 'c2s', 's2c' or None)")

        self.session_id = session_id
        self.direction = direction

        # Derives the AES and HMAC keys via HKDF-Expand.
        # The SKA session_key is already high-entropy → only Expand is used
        # (no Extract phase). Each info label is a domain separator: none
        # of the derived keys ever share cryptographic material.
        if direction is None:
            aes_key = self._derive(session_key, HKDF_INFO_AES)
            hmac_key = self._derive(session_key, HKDF_INFO_HMAC)
            self._enc_aes_key = self._dec_aes_key = aes_key
            self._enc_hmac_key = self._dec_hmac_key = hmac_key
        else:
            aes_c2s = self._derive(session_key, HKDF_INFO_AES_C2S)
            aes_s2c = self._derive(session_key, HKDF_INFO_AES_S2C)
            hmac_c2s = self._derive(session_key, HKDF_INFO_HMAC_C2S)
            hmac_s2c = self._derive(session_key, HKDF_INFO_HMAC_S2C)
            if direction == "c2s":
                self._enc_aes_key, self._dec_aes_key = aes_c2s, aes_s2c
                self._enc_hmac_key, self._dec_hmac_key = hmac_c2s, hmac_s2c
            else:
                self._enc_aes_key, self._dec_aes_key = aes_s2c, aes_c2s
                self._enc_hmac_key, self._dec_hmac_key = hmac_s2c, hmac_c2s

        logger.debug(
            "StlsTransport inizializzato: session_id=%s direction=%s",
            session_id, direction,
        )

    @staticmethod
    def _derive(session_key: bytes, info: bytes) -> bytes:
        return HKDFExpand(
            algorithm=hashes.SHA256(),
            length=32,
            info=info,
        ).derive(session_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: bytes) -> StlsEnvelope:
        """
        Encrypts an arbitrary payload.

        Pipeline:
            1. Generates a random 96-bit IV (12 bytes).
            2. Builds the AAD from (version, session_id) and feeds it to
               GCM, so both fields are bound to the ciphertext's integrity
               — not just carried alongside it.
            3. Encrypts with AES-256-GCM → ciphertext + GCM tag (128-bit).
            4. Computes HMAC-SHA256 over (AAD ‖ IV ‖ ciphertext ‖ GCM tag).
            5. Returns a StlsEnvelope with all components.

        Args:
            plaintext: Payload to encrypt (any bytes).

        Returns:
            StlsEnvelope ready for serialization and transport.
        """
        iv = os.urandom(12)  # 96-bit for AES-GCM
        aad = self._build_aad(STLS_VERSION, self.session_id)

        cipher = Cipher(
            algorithms.AES(self._enc_aes_key),
            modes.GCM(iv),
        )
        encryptor = cipher.encryptor()
        encryptor.authenticate_additional_data(aad)
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()
        tag = encryptor.tag  # 128-bit GCM authentication tag

        mac = self._compute_mac(self._enc_hmac_key, aad, iv, ciphertext, tag)

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
            version=STLS_VERSION,
        )

    def decrypt(self, envelope: StlsEnvelope) -> bytes:
        """
        Decrypts a STLS envelope.

        Pipeline:
            1. Verifies version and session_id (local, fail-fast checks).
            2. Rebuilds the AAD from the envelope's own (version, session_id)
               and verifies HMAC-SHA256 over (AAD ‖ IV ‖ ciphertext ‖ tag) —
               so tampering either field, even with iv/ciphertext/tag/mac
               otherwise left alone, is caught here before any decryption
               is attempted.
            3. Decrypts with AES-256-GCM, authenticating the same AAD
               (verifies the GCM tag internally).
            4. Returns the original plaintext.

        Args:
            envelope: StlsEnvelope received from the peer.

        Returns:
            Original plaintext as bytes.

        Raises:
            ValueError: If version/session_id don't match, or the HMAC /
                        GCM verification fails (integrity compromised).
        """
        if envelope.version != STLS_VERSION:
            raise ValueError(
                f"Unsupported envelope version: {envelope.version!r} "
                f"(expected {STLS_VERSION!r})"
            )

        if envelope.session_id != self.session_id:
            raise ValueError(
                f"Session ID mismatch: expected {self.session_id!r}, "
                f"got {envelope.session_id!r}"
            )

        aad = self._build_aad(envelope.version, envelope.session_id)

        # Verifies the HMAC before attempting decryption (fail-fast)
        expected_mac = self._compute_mac(
            self._dec_hmac_key, aad, envelope.iv, envelope.ciphertext, envelope.tag
        )
        if not self._constant_time_equal(expected_mac, envelope.mac):
            raise ValueError(
                "MAC verification failed — message integrity compromised"
            )

        # AES-256-GCM decryption (verifies the GCM tag + AAD internally)
        cipher = Cipher(
            algorithms.AES(self._dec_aes_key),
            modes.GCM(envelope.iv, envelope.tag),
        )
        decryptor = cipher.decryptor()
        decryptor.authenticate_additional_data(aad)
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

    @staticmethod
    def _compute_mac(
        hmac_key: bytes, aad: bytes, iv: bytes, ciphertext: bytes, tag: bytes
    ) -> bytes:
        """Computes HMAC-SHA256 over (AAD ‖ IV ‖ ciphertext ‖ GCM tag)."""
        h = hmac.HMAC(hmac_key, hashes.SHA256())
        h.update(aad)
        h.update(iv)
        h.update(ciphertext)
        h.update(tag)
        return h.finalize()

    @staticmethod
    def _build_aad(version: str, session_id: str) -> bytes:
        """
        Builds a self-delimiting AAD from (version, session_id), so that
        no two distinct (version, session_id) pairs can ever concatenate
        to the same bytes (length-prefixed fields instead of bare
        concatenation, which would let e.g. version="1" + session_id="0.a"
        collide with version="1.0" + session_id="a").
        """
        version_b = version.encode("utf-8")
        session_id_b = session_id.encode("utf-8")
        return (
            len(version_b).to_bytes(1, "big")
            + version_b
            + len(session_id_b).to_bytes(2, "big")
            + session_id_b
        )

    @staticmethod
    def _constant_time_equal(a: bytes, b: bytes) -> bool:
        """Timing-safe comparison between two byte strings."""
        return _hmac.compare_digest(a, b)
