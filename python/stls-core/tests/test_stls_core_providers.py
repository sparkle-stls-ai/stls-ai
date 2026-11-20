"""
test_stls_core_providers.py
----------------------------
Unit test per i key provider e le factory.

Copertura:
  P01 — KeyProviderBase.get_name() == "base"
  P02 — KeyProviderBase crea client e server senza errori
  P03 — KeySessionProviderBase SERVER: getSessionKeyIdentifier sincrono/async
  P04 — KeySessionProviderBase: getSessionKeyData restituisce bytes non vuoti
  P05 — KeySessionProviderBase: setSessionKeyIdentifier aggiorna il kid
  P06 — KeySessionProviderBase CLIENT e SERVER hanno session type corretto
  P07 — KeyProviderFactory.get_provider("base") restituisce KeyProviderBase
  P08 — KeyProviderFactory.get_provider("sconosciuto") → ValueError
  P09 — KeyProviderFactory è singleton
  P10 — KeySessionProviderFactory è singleton
  P11 — KeySessionProviderFactory crea provider SERVER base senza errori
  P12 — KeySessionProviderFactory crea provider CLIENT base senza errori
  P13 — KeyProviderFactory.get_provider("arqit_py") → ImportError se wheel assenti
  P14 — ARQIT_PY_DEVICE_ROLE=consumer + KEY_STORE_BACKEND=redis → KeySessionProviderArqitPy
        SERVER, senza richiedere i wheel [arqit_py] (issue #38)
  P15 — ARQIT_PY_DEVICE_ROLE=consumer + backend non-redis → ValueError
  P16 — ARQIT_PY_DEVICE_ROLE=consumer è ignorato per session_type=CLIENT
  P17 — ARQIT_PY_DEVICE_ROLE non impostato (default "owner") → comportamento inalterato
"""

import asyncio
import os

import pytest

from stls_core.providers.stls_core_providers_base import (
    KeyProvider,
    KeyProviderBase,
    KeyProviderClientBase,
    KeyProviderServerBase,
    KeySessionProvider,
    KeySessionProviderBase,
    _BASE_KEY_DATA,
    _BASE_KEY_ID,
)
from stls_core.providers.stls_core_providers_factory import (
    KeyProviderFactory,
    KeySessionProviderFactory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# P01 — get_name
# ---------------------------------------------------------------------------

def test_P01_base_provider_name():
    p = KeyProviderBase()
    assert p.get_name() == "base"


# ---------------------------------------------------------------------------
# P02 — create client/server
# ---------------------------------------------------------------------------

def test_P02_base_create_client():
    p = KeyProviderBase()
    client = p.create_provider_client(host="localhost", port=8091, options={})
    assert isinstance(client, KeyProviderClientBase)


def test_P02_base_create_server():
    p = KeyProviderBase()
    server = p.create_provider_server(port=8091, options={})
    assert isinstance(server, KeyProviderServerBase)


# ---------------------------------------------------------------------------
# P03 — getSessionKeyIdentifier
# ---------------------------------------------------------------------------

def test_P03_get_session_key_identifier(base_provider_session_server):
    kid = run(base_provider_session_server.getSessionKeyIdentifier())
    assert kid == _BASE_KEY_ID


# ---------------------------------------------------------------------------
# P04 — getSessionKeyData
# ---------------------------------------------------------------------------

def test_P04_get_session_key_data(base_provider_session_server):
    key = run(base_provider_session_server.getSessionKeyData(_BASE_KEY_ID))
    assert isinstance(key, bytes)
    assert len(key) > 0
    assert key == _BASE_KEY_DATA


# ---------------------------------------------------------------------------
# P05 — setSessionKeyIdentifier
# ---------------------------------------------------------------------------

def test_P05_set_session_key_identifier(base_provider_session_server):
    base_provider_session_server.setSessionKeyIdentifier("custom-kid")
    kid = run(base_provider_session_server.getSessionKeyIdentifier())
    assert kid == "custom-kid"


# ---------------------------------------------------------------------------
# P06 — session type
# ---------------------------------------------------------------------------

def test_P06_session_type_server(base_provider_session_server):
    assert base_provider_session_server.getSessionType() == \
        KeySessionProvider.KeySessionProviderType.SERVER


def test_P06_session_type_client(base_provider_session_client):
    assert base_provider_session_client.getSessionType() == \
        KeySessionProvider.KeySessionProviderType.CLIENT


# ---------------------------------------------------------------------------
# P07 — factory get_provider base
# ---------------------------------------------------------------------------

def test_P07_factory_get_base():
    factory = KeyProviderFactory()
    provider = factory.get_provider("base")
    assert isinstance(provider, KeyProviderBase)


# ---------------------------------------------------------------------------
# P08 — factory get_provider sconosciuto
# ---------------------------------------------------------------------------

def test_P08_factory_unknown_provider():
    factory = KeyProviderFactory()
    with pytest.raises(ValueError, match="Unsupported provider"):
        factory.get_provider("sconosciuto")


# ---------------------------------------------------------------------------
# P09 — KeyProviderFactory è singleton
# ---------------------------------------------------------------------------

def test_P09_factory_singleton():
    f1 = KeyProviderFactory()
    f2 = KeyProviderFactory()
    assert f1 is f2


# ---------------------------------------------------------------------------
# P10 — KeySessionProviderFactory è singleton
# ---------------------------------------------------------------------------

def test_P10_session_factory_singleton():
    f1 = KeySessionProviderFactory()
    f2 = KeySessionProviderFactory()
    assert f1 is f2


# ---------------------------------------------------------------------------
# P11 — KeySessionProviderFactory crea SERVER
# ---------------------------------------------------------------------------

def test_P11_session_factory_server(monkeypatch):
    monkeypatch.setenv("KEY_PROVIDER_NAME", "base")
    factory = KeySessionProviderFactory()
    provider = factory.get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8091,
    )
    assert provider is not None
    kid = run(provider.getSessionKeyIdentifier())
    assert kid == _BASE_KEY_ID


# ---------------------------------------------------------------------------
# P12 — KeySessionProviderFactory crea CLIENT
# ---------------------------------------------------------------------------

def test_P12_session_factory_client(monkeypatch):
    monkeypatch.setenv("KEY_PROVIDER_NAME", "base")
    factory = KeySessionProviderFactory()
    provider = factory.get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
        host="localhost",
        port=8091,
    )
    assert provider is not None
    kid = run(provider.getSessionKeyIdentifier())
    assert kid == _BASE_KEY_ID


# ---------------------------------------------------------------------------
# P13 — arqit_py → ImportError se wheel assenti
# ---------------------------------------------------------------------------

def test_P13_arqit_py_import_error():
    factory = KeyProviderFactory()
    try:
        import uk.arqit.device.common  # noqa
        pytest.skip("Wheel Arqit disponibili — test ImportError non applicabile")
    except ImportError:
        with pytest.raises(ImportError, match="arqit_py"):
            factory.get_provider("arqit_py")


# ---------------------------------------------------------------------------
# P14-P17 — ARQIT_PY_DEVICE_ROLE=consumer (issue #38)
# ---------------------------------------------------------------------------

def test_P14_consumer_role_returns_session_provider_without_wheels(monkeypatch):
    """P14 — consumer role builds a KeySessionProviderArqitPy SERVER session
    directly off the Redis-backed store, without ever needing the real
    uk.arqit.device.* SDK — forced absent here via sys.modules so this
    holds regardless of whether the [arqit_py] extra happens to be
    installed in the environment running this test.
    """
    import sys

    monkeypatch.setitem(sys.modules, "uk.arqit.device.common", None)
    monkeypatch.setenv("ARQIT_PY_DEVICE_ROLE", "consumer")
    monkeypatch.setenv("KEY_STORE_BACKEND", "redis")
    monkeypatch.setenv("KEY_STORE_REDIS_URL", "redis://localhost:6379")

    from unittest.mock import patch
    from stls_core.providers.stls_core_providers_arqit_py import KeySessionProviderArqitPy

    with patch("redis.Redis.from_url"):
        provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.SERVER,
            name="arqit_py",
        )
    assert isinstance(provider, KeySessionProviderArqitPy)
    assert provider.getSessionType() == KeySessionProvider.KeySessionProviderType.SERVER


def test_P15_consumer_role_requires_redis_backend(monkeypatch):
    """P15 — consumer role with a non-redis KEY_STORE_BACKEND raises ValueError:
    a consumer replica has no negotiation of its own, KEY_STORE_BACKEND=memory
    would leave it waiting forever for keys nobody populates.
    """
    monkeypatch.setenv("ARQIT_PY_DEVICE_ROLE", "consumer")
    monkeypatch.delenv("KEY_STORE_BACKEND", raising=False)

    with pytest.raises(ValueError, match="KEY_STORE_BACKEND=redis"):
        KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.SERVER,
            name="arqit_py",
        )


def test_P16_consumer_role_ignored_for_client_session_type(monkeypatch):
    """P16 — ARQIT_PY_DEVICE_ROLE=consumer has no effect on a CLIENT session:
    there's no "consumer client" concept, a client always negotiates its own
    key. Falls through to the normal path, which still requires the real
    SDK wheels -- forced absent here (see test_P14) so this holds
    regardless of the environment running this test.
    """
    import sys

    monkeypatch.setitem(sys.modules, "uk.arqit.device.common", None)
    monkeypatch.setenv("ARQIT_PY_DEVICE_ROLE", "consumer")
    with pytest.raises(ImportError, match="arqit_py"):
        KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            name="arqit_py",
        )


def test_P17_default_role_is_owner_unchanged_behavior(monkeypatch):
    """P17 — ARQIT_PY_DEVICE_ROLE unset (default "owner"): behavior identical
    to before this role even existed -- still requires the real SDK wheels
    for a SERVER session, same as test_P13 (forced absent here, see test_P14,
    so this holds regardless of the environment running this test).
    """
    import sys

    monkeypatch.setitem(sys.modules, "uk.arqit.device.common", None)
    monkeypatch.delenv("ARQIT_PY_DEVICE_ROLE", raising=False)
    with pytest.raises(ImportError, match="arqit_py"):
        KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.SERVER,
            name="arqit_py",
        )