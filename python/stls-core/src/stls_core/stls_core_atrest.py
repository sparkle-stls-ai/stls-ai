"""
stls_core_atrest.py
--------------------
Encryption-at-rest for values stls-core persists on Redis: the derived
keys in RedisSessionCache's payload and the raw key_data in
RedisArqitKeyStore. See issue #20.

AtRestCipher wraps a single value with AES-256-GCM under a key derived
(via HKDF-Expand, same primitive stls_core_crypto.py uses) from a
locally-held wrapping key (KEK) plus a per-component domain-separation
label — so one KEK can safely serve multiple independent stores without
key reuse across them.

This is independent of StlsTransport: StlsTransport models a two-party
session (HKDF direction-split, a separate HMAC-SHA256 layer over the
whole envelope) — overkill for sealing a single value for a single
reader (the same process/fleet that wrote it).

Opt-in: if the KEK env var is unset, AtRestCipher.from_env() returns
None and callers fall back to storing the value in the clear (logging a
warning) — no breaking change for existing Redis-backed deployments.
If the env var IS set but malformed, from_env() raises — a misconfigured
KEK should fail loudly, unlike an absent one.

Dependencies: cryptography>=46.0.5 (already a stls-core dependency).
"""

from __future__ import annotations

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

# Fixed AAD, shared with the Node port — binds every sealed blob to this
# specific scheme/version so the two implementations stay interoperable
# and a blob can't silently be reinterpreted under a different scheme.
_AAD = b"stls-core/at-rest/v1"
_KEK_LENGTH = 32
_NONCE_LENGTH = 12  # 96-bit, standard for AES-GCM


class AtRestCipher:
    """
    Seals/opens a single value at rest under a key derived from a KEK.

    Not reusable across processes with a different KEK, and not meant
    for anything beyond "encrypt a value before SET, decrypt it after
    GET" — see the module docstring.
    """

    def __init__(self, kek: bytes, label: bytes) -> None:
        """
        Args:
            kek:   The wrapping key, exactly 32 bytes.
            label: Domain-separation label (e.g. b"stls-core/session-cache"
                   or b"stls-core/keystore") so the same KEK can be reused
                   across independently-sealed stores.
        """
        if len(kek) != _KEK_LENGTH:
            raise ValueError(f"AtRestCipher: kek must be {_KEK_LENGTH} bytes, got {len(kek)}")
        aes_key = HKDFExpand(
            algorithm=hashes.SHA256(),
            length=_KEK_LENGTH,
            info=label,
        ).derive(kek)
        self._aesgcm = AESGCM(aes_key)

    @classmethod
    def from_env(
        cls, label: bytes, var_name: str = "STLS_CORE_ATREST_KEY"
    ) -> Optional["AtRestCipher"]:
        """
        Builds an AtRestCipher from a base64-encoded 32-byte KEK read from
        the environment.

        Returns:
            None if var_name is unset (caller decides the
            plaintext-fallback-with-warning behavior from that).

        Raises:
            ValueError: if var_name is set but isn't valid base64, or
                        doesn't decode to exactly 32 bytes.
        """
        raw = os.getenv(var_name)
        if raw is None or raw == "":
            return None

        try:
            kek = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise ValueError(
                f"{var_name} must be base64-encoded {_KEK_LENGTH} random bytes "
                f"(e.g. `openssl rand -base64 {_KEK_LENGTH}`): {exc}"
            ) from exc

        if len(kek) != _KEK_LENGTH:
            raise ValueError(
                f"{var_name} decodes to {len(kek)} bytes, expected {_KEK_LENGTH}"
            )

        return cls(kek, label)

    def seal(self, plaintext: bytes) -> bytes:
        """
        Encrypts plaintext with AES-256-GCM under a fresh random nonce.

        Returns:
            nonce (12 bytes) || ciphertext || GCM tag (16 bytes).
        """
        nonce = os.urandom(_NONCE_LENGTH)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, _AAD)
        return nonce + ciphertext

    def open(self, blob: bytes) -> bytes:
        """
        Decrypts a blob produced by seal().

        Raises:
            ValueError: if the blob is too short, the tag is invalid, or
                        the data is otherwise corrupted/tampered with.
        """
        if len(blob) < _NONCE_LENGTH:
            raise ValueError(f"AtRestCipher.open: blob too short ({len(blob)} bytes)")
        nonce, ciphertext = blob[:_NONCE_LENGTH], blob[_NONCE_LENGTH:]
        return self._aesgcm.decrypt(nonce, ciphertext, _AAD)
