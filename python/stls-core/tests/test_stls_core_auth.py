"""
test_stls_core_auth.py
-----------------------
Unit test per il sistema di autenticazione API key.

Copertura:
  A01 — hash_key produce SHA-256 hex digest di 64 caratteri
  A02 — hash_key è deterministico
  A03 — hash_key di valori diversi produce digest diversi
  A04 — constant_time_equal: uguali → True, diversi → False
  A05 — generate_api_key: formato prefisso corretto, key_id è SHA-256
  A06 — KeyRecord.is_expired: non scaduta → False, scaduta → True
  A07 — KeyRecord.allows_endpoint: wildcard, match, no-match, prefix
  A08 — KeyRecord.has_scope: wildcard, match, no-match
  A09 — KeyRecord.to_dict / from_dict round-trip
  A10 — ValidationResult.ok True solo se AuthResult.OK
  A11 — ValidationResult.www_authenticate_header per ogni AuthResult
  A12 — StaticLoader.load_all restituisce i KeyRecord configurati
  A13 — StaticLoader.load_one: hit e miss
  A14 — AuthResult enum ha tutti i valori attesi

  AuthConfig (dataclass statica, spostata da stls-a2a in questa sessione):
  A15 — AuthConfig() vuota: enabled è False (comportamento di default aperto)
  A16 — AuthConfig(api_keys=...): enabled è True
  A17 — AuthConfig(bearer_tokens=...): enabled è True
  A18 — check(): api_key corretta → True
  A19 — check(): api_key sbagliata → False
  A20 — check(): bearer token corretto ("Bearer <token>") → True
  A21 — check(): schema diverso da "Bearer" (es. "Basic") → False
  A22 — check(): Authorization malformato (niente spazio) → False
  A23 — check(): schema case-insensitive ("bearer"/"Bearer"/"BEARER")
  A24 — check(): nessuna credenziale fornita → sempre False
  A25 — check(): bearer_tokens vuoto → Authorization ignorato anche se presente
  A26 — check(): api_keys vuoto → X-API-Key ignorato anche se presente
  A27 — check(): entrambi gli schemi configurati, ciascuno basta da solo
  A28 — check(): match su una qualsiasi chiave di un set con più elementi
  A29 — realm: default "STLS", personalizzabile
  A30 — è una dataclass reale (comparabile per valore, non per identità)
"""

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from stls_core.auth.stls_core_auth_models import (
    AuthConfig,
    AuthResult,
    AuditEntry,
    KeyRecord,
    ValidationResult,
    constant_time_equal,
    generate_api_key,
    hash_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def make_record(
    raw_key: str = "stls_test_key_abc123",
    client_id: str = "test-client",
    scopes: list = None,
    endpoints: list = None,
    expires_at: datetime = None,
) -> KeyRecord:
    return KeyRecord(
        key_id=hash_key(raw_key),
        client_id=client_id,
        scopes=scopes or ["secure-stls"],
        endpoints=endpoints or [],
        expires_at=expires_at,
    )


def make_audit(result: AuthResult = AuthResult.OK) -> AuditEntry:
    return AuditEntry(
        timestamp=datetime.now(timezone.utc),
        key_id="test-kid",
        client_id="test-client",
        endpoint="/secure-a2a",
        result=result,
    )


# ---------------------------------------------------------------------------
# A01 — hash_key formato
# ---------------------------------------------------------------------------

def test_A01_hash_key_format():
    digest = hash_key("stls_test_key")
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ---------------------------------------------------------------------------
# A02 — hash_key deterministico
# ---------------------------------------------------------------------------

def test_A02_hash_key_deterministic():
    assert hash_key("same_key") == hash_key("same_key")


# ---------------------------------------------------------------------------
# A03 — hash_key valori diversi
# ---------------------------------------------------------------------------

def test_A03_hash_key_different_inputs():
    assert hash_key("key_A") != hash_key("key_B")


# ---------------------------------------------------------------------------
# A04 — constant_time_equal
# ---------------------------------------------------------------------------

def test_A04_constant_time_equal():
    assert constant_time_equal("abc", "abc") is True
    assert constant_time_equal("abc", "xyz") is False
    assert constant_time_equal("", "") is True
    assert constant_time_equal("a", "") is False


# ---------------------------------------------------------------------------
# A05 — generate_api_key
# ---------------------------------------------------------------------------

def test_A05_generate_api_key():
    raw, kid = generate_api_key(prefix="stls")
    assert raw.startswith("stls_")
    assert len(raw) > 10
    assert kid == hash_key(raw)
    assert len(kid) == 64

    # Due chiamate producono chiavi diverse
    raw2, kid2 = generate_api_key()
    assert raw != raw2
    assert kid != kid2


# ---------------------------------------------------------------------------
# A06 — KeyRecord.is_expired
# ---------------------------------------------------------------------------

def test_A06_not_expired():
    record = make_record(expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert record.is_expired() is False


def test_A06_expired():
    record = make_record(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert record.is_expired() is True


def test_A06_no_expiry():
    record = make_record(expires_at=None)
    assert record.is_expired() is False


# ---------------------------------------------------------------------------
# A07 — KeyRecord.allows_endpoint
# ---------------------------------------------------------------------------

def test_A07_endpoint_wildcard():
    record = make_record(endpoints=[])  # wildcard
    assert record.allows_endpoint("/secure-a2a") is True
    assert record.allows_endpoint("/qualsiasi/path") is True


def test_A07_endpoint_exact_match():
    record = make_record(endpoints=["/secure-a2a"])
    assert record.allows_endpoint("/secure-a2a") is True


def test_A07_endpoint_prefix_match():
    record = make_record(endpoints=["/secure-a2a"])
    assert record.allows_endpoint("/secure-a2a/stream") is True


def test_A07_endpoint_no_match():
    record = make_record(endpoints=["/secure-a2a"])
    assert record.allows_endpoint("/altro-path") is False


# ---------------------------------------------------------------------------
# A08 — KeyRecord.has_scope
# ---------------------------------------------------------------------------

def test_A08_scope_wildcard():
    record = make_record(scopes=["*"])
    assert record.has_scope("qualsiasi") is True
    assert record.has_scope("secure-stls") is True


def test_A08_scope_match():
    record = make_record(scopes=["secure-stls", "admin"])
    assert record.has_scope("secure-stls") is True
    assert record.has_scope("admin") is True


def test_A08_scope_no_match():
    record = make_record(scopes=["secure-stls"])
    assert record.has_scope("admin") is False


# ---------------------------------------------------------------------------
# A09 — KeyRecord to_dict / from_dict
# ---------------------------------------------------------------------------

def test_A09_key_record_serialization(sample_key_record):
    d = sample_key_record.to_dict()
    restored = KeyRecord.from_dict(d)

    assert restored.key_id == sample_key_record.key_id
    assert restored.client_id == sample_key_record.client_id
    assert restored.scopes == sample_key_record.scopes
    assert restored.endpoints == sample_key_record.endpoints
    assert restored.description == sample_key_record.description
    # expires_at: confronto con tolleranza 1 secondo per serializzazione ISO
    if sample_key_record.expires_at:
        diff = abs(
            (restored.expires_at - sample_key_record.expires_at).total_seconds()
        )
        assert diff < 1.0


# ---------------------------------------------------------------------------
# A10 — ValidationResult.ok
# ---------------------------------------------------------------------------

def test_A10_validation_result_ok():
    record = make_record()
    audit = make_audit(AuthResult.OK)
    vr = ValidationResult(result=AuthResult.OK, record=record, audit=audit)
    assert vr.ok is True


def test_A10_validation_result_not_ok():
    for result in [AuthResult.INVALID, AuthResult.EXPIRED,
                   AuthResult.REVOKED, AuthResult.SCOPE_DENIED]:
        audit = make_audit(result)
        vr = ValidationResult(result=result, record=None, audit=audit)
        assert vr.ok is False


# ---------------------------------------------------------------------------
# A11 — ValidationResult.www_authenticate_header
# ---------------------------------------------------------------------------

def test_A11_www_authenticate_headers():
    def make_vr(r):
        return ValidationResult(
            result=r, record=None, audit=make_audit(r)
        )

    assert "token_expired" in make_vr(AuthResult.EXPIRED).www_authenticate_header()
    assert "token_revoked" in make_vr(AuthResult.REVOKED).www_authenticate_header()
    assert "insufficient_scope" in make_vr(AuthResult.SCOPE_DENIED).www_authenticate_header()
    header = make_vr(AuthResult.INVALID).www_authenticate_header()
    assert "ApiKey" in header


# ---------------------------------------------------------------------------
# A12 — StaticLoader.load_all
# ---------------------------------------------------------------------------

def test_A12_static_loader_load_all(sample_key_record):
    from stls_core.auth.stls_core_auth_loaders import StaticLoader
    loader = StaticLoader(records=[sample_key_record])
    records = run(loader.load_all())
    assert len(records) == 1
    assert records[0].key_id == sample_key_record.key_id
    assert records[0].client_id == sample_key_record.client_id


# ---------------------------------------------------------------------------
# A13 — StaticLoader.load_one
# ---------------------------------------------------------------------------

def test_A13_static_loader_load_one_hit(sample_key_record):
    from stls_core.auth.stls_core_auth_loaders import StaticLoader
    loader = StaticLoader(records=[sample_key_record])
    record = run(loader.load_one(sample_key_record.key_id))
    assert record is not None
    assert record.key_id == sample_key_record.key_id


def test_A13_static_loader_load_one_miss(sample_key_record):
    from stls_core.auth.stls_core_auth_loaders import StaticLoader
    loader = StaticLoader(records=[sample_key_record])
    record = run(loader.load_one("non-esiste-key-id-xxxxxxxx"))
    assert record is None


# ---------------------------------------------------------------------------
# A14 — AuthResult enum valori
# ---------------------------------------------------------------------------

def test_A14_auth_result_values():
    expected = {"ok", "invalid", "revoked", "expired", "scope_denied", "internal_error"}
    actual = {r.value for r in AuthResult}
    assert actual == expected

# ---------------------------------------------------------------------------
# A15-A17 — AuthConfig.enabled
# ---------------------------------------------------------------------------

def test_A15_authconfig_empty_disabled():
    cfg = AuthConfig()
    assert cfg.enabled is False


def test_A16_authconfig_api_keys_enabled():
    cfg = AuthConfig(api_keys={"key-1"})
    assert cfg.enabled is True


def test_A17_authconfig_bearer_tokens_enabled():
    cfg = AuthConfig(bearer_tokens={"tok-1"})
    assert cfg.enabled is True


# ---------------------------------------------------------------------------
# A18-A19 — AuthConfig.check() con API key
# ---------------------------------------------------------------------------

def test_A18_check_api_key_correct():
    cfg = AuthConfig(api_keys={"secret-key-1"})
    assert cfg.check(None, "secret-key-1") is True


def test_A19_check_api_key_wrong():
    cfg = AuthConfig(api_keys={"secret-key-1"})
    assert cfg.check(None, "wrong-key") is False


# ---------------------------------------------------------------------------
# A20-A23 — AuthConfig.check() con bearer token
# ---------------------------------------------------------------------------

def test_A20_check_bearer_correct():
    cfg = AuthConfig(bearer_tokens={"tok-abc"})
    assert cfg.check("Bearer tok-abc", None) is True


def test_A21_check_bearer_wrong_scheme():
    cfg = AuthConfig(bearer_tokens={"tok-abc"})
    assert cfg.check("Basic tok-abc", None) is False


def test_A22_check_bearer_malformed_no_space():
    cfg = AuthConfig(bearer_tokens={"tok-abc"})
    assert cfg.check("tok-abc", None) is False


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_A23_check_bearer_scheme_case_insensitive(scheme):
    cfg = AuthConfig(bearer_tokens={"tok-abc"})
    assert cfg.check(f"{scheme} tok-abc", None) is True


# ---------------------------------------------------------------------------
# A24 — AuthConfig.check() senza credenziali
# ---------------------------------------------------------------------------

def test_A24_check_no_credentials():
    cfg = AuthConfig(api_keys={"k"}, bearer_tokens={"t"})
    assert cfg.check(None, None) is False


# ---------------------------------------------------------------------------
# A25-A26 — schema non configurato → header corrispondente ignorato
# ---------------------------------------------------------------------------

def test_A25_check_bearer_ignored_if_not_configured():
    # Solo api_keys configurato — un Authorization valido non deve bastare
    cfg = AuthConfig(api_keys={"k"})
    assert cfg.check("Bearer qualunque-token", None) is False


def test_A26_check_api_key_ignored_if_not_configured():
    # Solo bearer_tokens configurato — una X-API-Key valida non deve bastare
    cfg = AuthConfig(bearer_tokens={"t"})
    assert cfg.check(None, "qualunque-chiave") is False


# ---------------------------------------------------------------------------
# A27 — entrambi gli schemi configurati insieme
# ---------------------------------------------------------------------------

def test_A27_check_both_schemes_either_suffices():
    cfg = AuthConfig(api_keys={"key-1"}, bearer_tokens={"tok-1"})
    assert cfg.check(None, "key-1") is True             # solo api key
    assert cfg.check("Bearer tok-1", None) is True       # solo bearer
    assert cfg.check("Bearer tok-1", "key-1") is True    # entrambi
    assert cfg.check("Bearer wrong", "wrong") is False   # nessuno dei due


# ---------------------------------------------------------------------------
# A28 — match su una tra più chiavi
# ---------------------------------------------------------------------------

def test_A28_check_matches_any_key_in_set():
    cfg = AuthConfig(api_keys={"key-1", "key-2", "key-3"})
    assert cfg.check(None, "key-1") is True
    assert cfg.check(None, "key-2") is True
    assert cfg.check(None, "key-3") is True
    assert cfg.check(None, "key-4") is False


# ---------------------------------------------------------------------------
# A29 — realm
# ---------------------------------------------------------------------------

def test_A29_realm_default():
    cfg = AuthConfig()
    assert cfg.realm == "STLS"


def test_A29_realm_custom():
    cfg = AuthConfig(realm="My Server")
    assert cfg.realm == "My Server"


# ---------------------------------------------------------------------------
# A30 — è una dataclass reale (equality by value)
# ---------------------------------------------------------------------------

def test_A30_authconfig_is_dataclass_value_equality():
    cfg1 = AuthConfig(api_keys={"k"}, realm="X")
    cfg2 = AuthConfig(api_keys={"k"}, realm="X")
    assert cfg1 == cfg2  # dataclass: uguaglianza per valore, non identità
    assert cfg1 is not cfg2