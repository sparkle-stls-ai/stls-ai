"""
conftest.py
-----------
Fixture condivise per tutti i test di stls-core.
"""

import asyncio
import os

import pytest


# ---------------------------------------------------------------------------
# Fixtures — crypto
# ---------------------------------------------------------------------------

@pytest.fixture
def session_key_16() -> bytes:
    """Chiave di sessione minima (16 byte)."""
    return b"0123456789abcdef"


@pytest.fixture
def session_key_32() -> bytes:
    """Chiave di sessione standard (32 byte / 256 bit)."""
    return os.urandom(32)


@pytest.fixture
def session_id() -> str:
    """Session ID fisso per test deterministici."""
    return "test-session-id-0001"


@pytest.fixture
def stls_transport(session_key_32, session_id):
    """StlsTransport pronto per encrypt/decrypt."""
    from stls_core.stls_core_crypto import StlsTransport
    return StlsTransport(session_key=session_key_32, session_id=session_id)


# ---------------------------------------------------------------------------
# Fixtures — session cache
# ---------------------------------------------------------------------------

@pytest.fixture
def session_cache():
    """InMemorySessionCache con dimensioni ridotte per test di eviction."""
    from stls_core.stls_core_session import InMemorySessionCache
    return InMemorySessionCache(max_size=4, ttl_seconds=10)


# ---------------------------------------------------------------------------
# Fixtures — providers
# ---------------------------------------------------------------------------

@pytest.fixture
def base_provider_session_server():
    """KeySessionProvider Base lato SERVER."""
    from stls_core.providers.stls_core_providers_base import (
        KeySessionProvider,
        KeySessionProviderBase,
    )
    return KeySessionProviderBase(KeySessionProvider.KeySessionProviderType.SERVER)


@pytest.fixture
def base_provider_session_client():
    """KeySessionProvider Base lato CLIENT."""
    from stls_core.providers.stls_core_providers_base import (
        KeySessionProvider,
        KeySessionProviderBase,
    )
    return KeySessionProviderBase(KeySessionProvider.KeySessionProviderType.CLIENT)


# ---------------------------------------------------------------------------
# Fixtures — auth
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_raw_key() -> str:
    """API key raw di esempio."""
    return "stls_test_" + "a" * 32


@pytest.fixture
def sample_key_record(sample_raw_key):
    """KeyRecord corrispondente alla sample_raw_key."""
    from stls_core.auth.stls_core_auth_models import KeyRecord, hash_key
    from datetime import datetime, timezone, timedelta
    return KeyRecord(
        key_id=hash_key(sample_raw_key),
        client_id="test-client",
        scopes=["secure-stls", "*"],
        endpoints=[],          # wildcard — tutti gli endpoint
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        description="Test key",
    )