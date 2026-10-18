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
    with pytest.raises(ValueError, match="non supportato"):
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