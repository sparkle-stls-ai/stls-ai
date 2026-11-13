"""
test_stls_core_atrest.py
--------------------------
Unit test per AtRestCipher (encryption-at-rest wrapper, issue #20).

Copertura:
  A01 — seal()/open() round-trip
  A02 — nonce is random: two seal() calls on the same plaintext differ
  A03 — tampered blob → open() raises
  A04 — wrong KEK → open() raises
  A05 — different labels (same KEK) derive independent keys
  A06 — from_env() returns None when the env var is unset
  A07 — from_env() raises on invalid base64
  A08 — from_env() raises when the decoded KEK isn't 32 bytes
  A09 — from_env() builds a working cipher when the env var is valid
  A10 — constructor rejects a KEK that isn't 32 bytes
  A11 — open() rejects a blob shorter than the nonce
"""

from __future__ import annotations

import base64

import pytest

from stls_core.stls_core_atrest import AtRestCipher

KEK = b"\x11" * 32
LABEL = b"stls-core/test"
PLAINTEXT = b'{"hello":"world"}'


def test_A01_roundtrip():
    cipher = AtRestCipher(KEK, LABEL)
    assert cipher.open(cipher.seal(PLAINTEXT)) == PLAINTEXT


def test_A02_nonce_is_random():
    cipher = AtRestCipher(KEK, LABEL)
    blob1 = cipher.seal(PLAINTEXT)
    blob2 = cipher.seal(PLAINTEXT)
    assert blob1 != blob2


def test_A03_tampered_blob_raises():
    cipher = AtRestCipher(KEK, LABEL)
    blob = bytearray(cipher.seal(PLAINTEXT))
    blob[-1] ^= 0xFF
    with pytest.raises(Exception):
        cipher.open(bytes(blob))


def test_A04_wrong_kek_raises():
    sealed = AtRestCipher(KEK, LABEL).seal(PLAINTEXT)
    other = AtRestCipher(b"\x22" * 32, LABEL)
    with pytest.raises(Exception):
        other.open(sealed)


def test_A05_different_labels_are_independent():
    sealed = AtRestCipher(KEK, b"label-a").seal(PLAINTEXT)
    other = AtRestCipher(KEK, b"label-b")
    with pytest.raises(Exception):
        other.open(sealed)


def test_A06_from_env_unset_returns_none(monkeypatch):
    monkeypatch.delenv("STLS_CORE_ATREST_KEY", raising=False)
    assert AtRestCipher.from_env(LABEL) is None


def test_A07_from_env_invalid_base64_raises(monkeypatch):
    monkeypatch.setenv("STLS_CORE_ATREST_KEY", "not-valid-base64!!")
    with pytest.raises(ValueError):
        AtRestCipher.from_env(LABEL)


def test_A08_from_env_wrong_length_raises(monkeypatch):
    monkeypatch.setenv("STLS_CORE_ATREST_KEY", base64.b64encode(b"\x00" * 16).decode())
    with pytest.raises(ValueError):
        AtRestCipher.from_env(LABEL)


def test_A09_from_env_valid_builds_working_cipher(monkeypatch):
    monkeypatch.setenv("STLS_CORE_ATREST_KEY", base64.b64encode(KEK).decode())
    cipher = AtRestCipher.from_env(LABEL)
    assert cipher is not None
    assert cipher.open(cipher.seal(PLAINTEXT)) == PLAINTEXT


def test_A10_constructor_rejects_wrong_kek_length():
    with pytest.raises(ValueError):
        AtRestCipher(b"\x00" * 16, LABEL)


def test_A11_open_rejects_short_blob():
    cipher = AtRestCipher(KEK, LABEL)
    with pytest.raises(ValueError):
        cipher.open(b"\x00" * 4)
