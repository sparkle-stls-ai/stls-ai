"""
test_stls_core_crypto.py
-------------------------
Unit test per StlsTransport e StlsEnvelope.

Copertura:
  T01 — encrypt produce envelope con tutti i campi obbligatori
  T02 — decrypt(encrypt(plaintext)) == plaintext (round-trip)
  T03 — IV è random: due encrypt dello stesso plaintext producono IV diversi
  T04 — session_id mismatch → ValueError
  T05 — MAC alterato → ValueError
  T06 — ciphertext alterato → ValueError (GCM tag fallisce)
  T07 — encrypt_json / decrypt_json round-trip su dict
  T08 — encrypt_json / decrypt_json round-trip su lista
  T09 — chiavi di encrypt e di MAC derivate sono diverse tra loro
  T10 — due transport con stessa key+session_id producono chiavi derivate identiche
  T11 — StlsEnvelope.to_dict / from_dict round-trip
  T12 — plaintext vuoto cifra e decifra correttamente
  T13 — plaintext grande (1 MB) cifra e decifra correttamente
  T14 — version alterato nell'envelope → ValueError (AAD/version ora autenticati)
  T15 — session_id alterato nell'envelope (via dict, non il check locale) → ValueError
  T16 — direction="c2s"/"s2c": round-trip bidirezionale tra due transport
  T17 — direction: le chiavi delle due direzioni sono indipendenti (incrociate correttamente
        tra i due transport, un transport non può decifrare il proprio stesso output)
  T18 — direction=None (default) e direction="c2s" derivano chiavi diverse (nessuna
        confusione tra le due modalità)
  T19 — direction invalido → ValueError
"""

import json
import os

import pytest

from stls_core.stls_core_crypto import StlsEnvelope, StlsTransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_transport(key: bytes = None, sid: str = "sid-test") -> StlsTransport:
    if key is None:
        key = os.urandom(32)
    return StlsTransport(session_key=key, session_id=sid)


# ---------------------------------------------------------------------------
# T01 — envelope fields
# ---------------------------------------------------------------------------

def test_T01_envelope_has_all_fields(stls_transport):
    envelope = stls_transport.encrypt(b"hello")
    d = envelope.to_dict()
    assert "version" in d
    assert "session_id" in d
    assert "iv" in d
    assert "ciphertext" in d
    assert "tag" in d
    assert "mac" in d
    assert d["version"] == "1.0"
    assert d["session_id"] == stls_transport.session_id


# ---------------------------------------------------------------------------
# T02 — round-trip
# ---------------------------------------------------------------------------

def test_T02_roundtrip_bytes(stls_transport):
    plaintext = b"messaggio di test 1234"
    envelope = stls_transport.encrypt(plaintext)
    recovered = stls_transport.decrypt(envelope)
    assert recovered == plaintext


def test_T02_roundtrip_unicode(stls_transport):
    plaintext = "Ciao mondo 🌍".encode("utf-8")
    envelope = stls_transport.encrypt(plaintext)
    recovered = stls_transport.decrypt(envelope)
    assert recovered == plaintext


# ---------------------------------------------------------------------------
# T03 — IV randomness
# ---------------------------------------------------------------------------

def test_T03_iv_is_random(stls_transport):
    e1 = stls_transport.encrypt(b"same payload")
    e2 = stls_transport.encrypt(b"same payload")
    assert e1.iv != e2.iv
    assert e1.ciphertext != e2.ciphertext


# ---------------------------------------------------------------------------
# T04 — session_id mismatch
# ---------------------------------------------------------------------------

def test_T04_session_id_mismatch(session_key_32):
    t1 = StlsTransport(session_key_32, "session-A")
    t2 = StlsTransport(session_key_32, "session-B")
    envelope = t1.encrypt(b"payload")
    with pytest.raises(ValueError, match="Session ID mismatch"):
        t2.decrypt(envelope)


# ---------------------------------------------------------------------------
# T05 — MAC tampered
# ---------------------------------------------------------------------------

def test_T05_mac_tampered(stls_transport):
    envelope = stls_transport.encrypt(b"payload")
    # Altera un byte del MAC
    bad_mac = bytes([envelope.mac[0] ^ 0xFF]) + envelope.mac[1:]
    tampered = StlsEnvelope(
        session_id=envelope.session_id,
        iv=envelope.iv,
        ciphertext=envelope.ciphertext,
        tag=envelope.tag,
        mac=bad_mac,
    )
    with pytest.raises(ValueError, match="MAC verification failed"):
        stls_transport.decrypt(tampered)


# ---------------------------------------------------------------------------
# T06 — ciphertext tampered (GCM tag fails)
# ---------------------------------------------------------------------------

def test_T06_ciphertext_tampered(stls_transport):
    envelope = stls_transport.encrypt(b"payload originale")
    # Altera il ciphertext, lasciando il MAC originale: il check HMAC (che
    # copre anche il ciphertext) deve fallire prima ancora di arrivare al GCM.
    bad_ct = bytes([envelope.ciphertext[0] ^ 0x01]) + envelope.ciphertext[1:]
    tampered = StlsEnvelope(
        session_id=envelope.session_id,
        iv=envelope.iv,
        ciphertext=bad_ct,
        tag=envelope.tag,
        mac=envelope.mac,
    )
    with pytest.raises(ValueError, match="MAC verification failed"):
        stls_transport.decrypt(tampered)


# ---------------------------------------------------------------------------
# T07 / T08 — encrypt_json / decrypt_json
# ---------------------------------------------------------------------------

def test_T07_json_roundtrip_dict(stls_transport):
    payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
               "params": {"name": "get_weather", "city": "Roma"}}
    wire = stls_transport.encrypt_json(payload)
    assert isinstance(wire, dict)
    recovered = stls_transport.decrypt_json(wire)
    assert recovered == payload


def test_T08_json_roundtrip_list(stls_transport):
    payload = [1, "due", {"tre": 3}, None, True]
    wire = stls_transport.encrypt_json(payload)
    recovered = stls_transport.decrypt_json(wire)
    assert recovered == payload


# ---------------------------------------------------------------------------
# T09 — AES key e HMAC key diverse
# ---------------------------------------------------------------------------

def test_T09_derived_keys_differ(stls_transport):
    assert stls_transport._enc_aes_key != stls_transport._enc_hmac_key
    # Modalità default (direction=None): stessa chiave per encrypt e decrypt.
    assert stls_transport._enc_aes_key == stls_transport._dec_aes_key
    assert stls_transport._enc_hmac_key == stls_transport._dec_hmac_key


# ---------------------------------------------------------------------------
# T10 — chiavi derivate deterministiche
# ---------------------------------------------------------------------------

def test_T10_deterministic_key_derivation(session_key_32, session_id):
    t1 = StlsTransport(session_key_32, session_id)
    t2 = StlsTransport(session_key_32, session_id)
    assert t1._enc_aes_key == t2._enc_aes_key
    assert t1._enc_hmac_key == t2._enc_hmac_key


# ---------------------------------------------------------------------------
# T11 — StlsEnvelope to_dict / from_dict
# ---------------------------------------------------------------------------

def test_T11_envelope_serialization(stls_transport):
    envelope = stls_transport.encrypt(b"test")
    d = envelope.to_dict()
    restored = StlsEnvelope.from_dict(d)
    assert restored.session_id == envelope.session_id
    assert restored.iv == envelope.iv
    assert restored.ciphertext == envelope.ciphertext
    assert restored.tag == envelope.tag
    assert restored.mac == envelope.mac
    # Verifica che il transport decifri correttamente dall'envelope restaurato
    assert stls_transport.decrypt(restored) == b"test"


# ---------------------------------------------------------------------------
# T12 — plaintext vuoto
# ---------------------------------------------------------------------------

def test_T12_empty_plaintext(stls_transport):
    envelope = stls_transport.encrypt(b"")
    recovered = stls_transport.decrypt(envelope)
    assert recovered == b""


# ---------------------------------------------------------------------------
# T13 — plaintext grande (1 MB)
# ---------------------------------------------------------------------------

def test_T13_large_plaintext(stls_transport):
    plaintext = os.urandom(1024 * 1024)  # 1 MB
    envelope = stls_transport.encrypt(plaintext)
    recovered = stls_transport.decrypt(envelope)
    assert recovered == plaintext


# ---------------------------------------------------------------------------
# T14 — version alterato nell'envelope → ValueError
# ---------------------------------------------------------------------------

def test_T14_version_tampered(stls_transport):
    envelope = stls_transport.encrypt(b"payload")
    wire = envelope.to_dict()
    wire["version"] = "9.9"  # MAC/AAD originali sono su version="1.0"
    tampered = StlsEnvelope.from_dict(wire)
    with pytest.raises(ValueError):
        stls_transport.decrypt(tampered)


# ---------------------------------------------------------------------------
# T15 — session_id alterato nell'envelope (via dict) → ValueError
# ---------------------------------------------------------------------------

def test_T15_session_id_field_tampered(stls_transport):
    envelope = stls_transport.encrypt(b"payload")
    wire = envelope.to_dict()
    wire["session_id"] = stls_transport.session_id + "-tampered"
    tampered = StlsEnvelope.from_dict(wire)
    with pytest.raises(ValueError, match="Session ID mismatch"):
        stls_transport.decrypt(tampered)


# ---------------------------------------------------------------------------
# T16 — direction c2s/s2c: round-trip bidirezionale
# ---------------------------------------------------------------------------

def test_T16_direction_roundtrip(session_key_32, session_id):
    client = StlsTransport(session_key_32, session_id, direction="c2s")
    server = StlsTransport(session_key_32, session_id, direction="s2c")

    request_envelope = client.encrypt(b"richiesta dal client")
    assert server.decrypt(request_envelope) == b"richiesta dal client"

    response_envelope = server.encrypt(b"risposta dal server")
    assert client.decrypt(response_envelope) == b"risposta dal server"


# ---------------------------------------------------------------------------
# T17 — direction: chiavi delle due direzioni indipendenti
# ---------------------------------------------------------------------------

def test_T17_direction_keys_are_independent(session_key_32, session_id):
    client = StlsTransport(session_key_32, session_id, direction="c2s")
    server = StlsTransport(session_key_32, session_id, direction="s2c")

    # Le due direzioni concordano: la chiave di encrypt di un lato è la
    # chiave di decrypt dell'altro (altrimenti il round-trip di T16 non
    # funzionerebbe) — verificato qui a livello di chiave, non solo di esito.
    assert client._enc_aes_key == server._dec_aes_key
    assert server._enc_aes_key == client._dec_aes_key
    assert client._enc_hmac_key == server._dec_hmac_key
    assert server._enc_hmac_key == client._dec_hmac_key

    # Le due direzioni non condividono materiale crittografico tra loro.
    assert client._enc_aes_key != client._dec_aes_key
    assert client._enc_hmac_key != client._dec_hmac_key

    # Un transport non può decifrare il proprio stesso output: la chiave di
    # encrypt e quella di decrypt sono diverse per costruzione.
    envelope = client.encrypt(b"solo per il server")
    with pytest.raises(ValueError, match="MAC verification failed"):
        client.decrypt(envelope)


# ---------------------------------------------------------------------------
# T18 — direction=None vs direction="c2s": chiavi diverse, nessuna confusione
# ---------------------------------------------------------------------------

def test_T18_legacy_mode_keys_differ_from_direction_mode(session_key_32, session_id):
    legacy = StlsTransport(session_key_32, session_id)
    labeled = StlsTransport(session_key_32, session_id, direction="c2s")

    assert legacy._enc_aes_key != labeled._enc_aes_key
    assert legacy._enc_hmac_key != labeled._enc_hmac_key

    # Un envelope della modalità legacy non è decifrabile dalla modalità labeled.
    envelope = legacy.encrypt(b"legacy payload")
    with pytest.raises(ValueError, match="MAC verification failed"):
        labeled.decrypt(envelope)


# ---------------------------------------------------------------------------
# T19 — direction invalido → ValueError
# ---------------------------------------------------------------------------

def test_T19_invalid_direction_rejected(session_key_32, session_id):
    with pytest.raises(ValueError, match="Invalid direction"):
        StlsTransport(session_key_32, session_id, direction="bogus")