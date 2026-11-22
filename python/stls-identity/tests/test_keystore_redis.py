"""
keystore.py's RedisIdentityKeystore against a REAL Redis instance — no
mocks. Not covered anywhere before this file (issue #49): only
InMemoryIdentityKeystore was ever exercised by the rest of the suite.

Requires a Redis instance reachable at STATUS_LIST_TEST_REDIS_URL
(default redis://localhost:6379/15 — same isolated test DB the rest of
this package's Redis-backed tests already use). Deletes only this
file's own namespaced keys, never flushdb — no need to, and no reason
to risk another test file's data in the same DB.
"""

import base64
import os
from urllib.parse import urlparse

import pytest

from stls_identity import RedisIdentityKeystore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REDIS_URL = os.getenv("STATUS_LIST_TEST_REDIS_URL", "redis://localhost:6379/15")

# Same safety guard as test_status_list_redis.py — refuse to run
# against the production-default DB index even if the env var is ever
# misconfigured again.
_db_path = urlparse(REDIS_URL).path.lstrip("/")
if _db_path in ("", "0"):
    raise RuntimeError(
        f"STATUS_LIST_TEST_REDIS_URL={REDIS_URL!r} resolves to Redis DB "
        f"{_db_path or '0'} (the default production DB) — refusing to run."
    )

_NAMESPACE = "test:identity:signingkey"


@pytest.fixture
def keystore(monkeypatch):
    monkeypatch.delenv("STLS_CORE_ATREST_KEY", raising=False)
    ks = RedisIdentityKeystore(redis_url=REDIS_URL, namespace=_NAMESPACE)
    yield ks
    for key in ks._r.scan_iter(match=f"{_NAMESPACE}:*"):
        ks._r.delete(key)
    ks._r.close()


@pytest.fixture
def encrypted_keystore(monkeypatch):
    monkeypatch.setenv("STLS_CORE_ATREST_KEY", base64.b64encode(os.urandom(32)).decode())
    ks = RedisIdentityKeystore(redis_url=REDIS_URL, namespace=_NAMESPACE)
    yield ks
    for key in ks._r.scan_iter(match=f"{_NAMESPACE}:*"):
        ks._r.delete(key)
    ks._r.close()


def test_get_missing_returns_none(keystore):
    assert keystore.get("nobody-here") is None


def test_put_then_get_roundtrips_plaintext(keystore, caplog):
    """No STLS_CORE_ATREST_KEY: stored raw, logs a warning once at
    construction (not on every put/get — see RedisIdentityKeystore.__init__)."""
    key = Ed25519PrivateKey.generate()
    keystore.put("agent-087", key)

    fetched = keystore.get("agent-087")
    assert fetched.private_bytes_raw() == key.private_bytes_raw()

    raw_stored = keystore._r.get(keystore._key("agent-087"))
    assert len(raw_stored) == 32, "no cipher configured: stored bytes must be the raw 32-byte key"


def test_put_then_get_roundtrips_encrypted(encrypted_keystore):
    key = Ed25519PrivateKey.generate()
    encrypted_keystore.put("agent-087", key)

    fetched = encrypted_keystore.get("agent-087")
    assert fetched.private_bytes_raw() == key.private_bytes_raw()

    raw_stored = encrypted_keystore._r.get(encrypted_keystore._key("agent-087"))
    assert raw_stored != key.private_bytes_raw(), "stored bytes must not be the plaintext key"
    assert len(raw_stored) == 32 + 12 + 16, "nonce (12) + ciphertext (32) + GCM tag (16)"


def test_wrong_kek_fails_closed(monkeypatch):
    """Simulates a key rotated/lost KEK: get() must fail closed (return
    None, logging the error) rather than raise out to the caller or
    silently return garbage key material — same posture as stls-core's
    own RedisKeyStore.get()."""
    monkeypatch.setenv("STLS_CORE_ATREST_KEY", base64.b64encode(os.urandom(32)).decode())
    writer = RedisIdentityKeystore(redis_url=REDIS_URL, namespace=_NAMESPACE)
    key = Ed25519PrivateKey.generate()
    writer.put("agent-wrong-kek", key)

    monkeypatch.setenv("STLS_CORE_ATREST_KEY", base64.b64encode(os.urandom(32)).decode())
    reader = RedisIdentityKeystore(redis_url=REDIS_URL, namespace=_NAMESPACE)
    assert reader.get("agent-wrong-kek") is None

    for k in writer._r.scan_iter(match=f"{_NAMESPACE}:*"):
        writer._r.delete(k)
    writer._r.close()
    reader._r.close()
