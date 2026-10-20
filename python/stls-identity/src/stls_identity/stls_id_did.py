"""
stls_identity.did
==================

Bootstraps a did:web identity for an STLS agent.

Deliberately NOT reusing the agent's existing X25519 key (see the note in
models.py): a fresh Ed25519 signing keypair is generated here instead.

Correction from an earlier draft of this module: the private key is now
actually persisted, via keystore.py — NOT stls_core's ArqitKeyStoreBase
(that store is scoped to ephemeral SKA session keys with TTL/eviction
semantics, wrong fit for a long-lived identity key; see keystore.py's
docstring for the full reasoning). stls-identity owns its own small,
TTL-free keystore for this.

did:web resolution rule (https://w3c-ccg.github.io/did-method-web/):
    did:web:sofia.sparkle.example.com:agents:087
        -> https://sofia.sparkle.example.com/agents/087/did.json
    did:web:sofia.sparkle.example.com
        -> https://sofia.sparkle.example.com/.well-known/did.json
"""

from __future__ import annotations

import datetime as dt

import base58
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

from typing import TYPE_CHECKING

from .stls_id_models import DIDDocument, VerificationMethod

if TYPE_CHECKING:
    from .stls_id_keystore import IdentityKeystoreBase

_MULTIBASE_ED25519_PUB_PREFIX = b"\xed\x01"  # multicodec ed25519-pub, per did:key/multibase convention


def _multibase_encode_ed25519_pub(raw_pub: bytes) -> str:
    """base58btc, 'z' prefix — the multibase encoding W3C examples use for
    Ed25519VerificationKey2020. Swap for your project's base58 helper if
    one already exists in stls-core; kept self-contained here."""
    return "z" + base58.b58encode(_MULTIBASE_ED25519_PUB_PREFIX + raw_pub).decode("ascii")


def decode_multibase_ed25519_public_key(multibase_str: str) -> Ed25519PublicKey:
    """
    Inverse of _multibase_encode_ed25519_pub — reconstructs a usable
    Ed25519PublicKey object from a DID Document's publicKeyMultibase
    string. Public (unlike its encode counterpart) because the trust
    registry needs it to turn a stored/resolved key back into something
    Sentinel can actually verify a signature with.
    """
    if not multibase_str.startswith("z"):
        raise ValueError(f"expected multibase 'z' (base58btc) prefix, got: {multibase_str[:1]!r}")

    raw = base58.b58decode(multibase_str[1:])
    prefix, key_bytes = raw[:2], raw[2:]
    if prefix != _MULTIBASE_ED25519_PUB_PREFIX:
        raise ValueError(
            f"unexpected multicodec prefix {prefix!r}, expected {_MULTIBASE_ED25519_PUB_PREFIX!r} "
            "(ed25519-pub) — this key may not be an Ed25519 public key"
        )
    return Ed25519PublicKey.from_public_bytes(key_bytes)


def domain_and_path_to_did(host: str, *path_segments: str) -> str:
    """'sofia.sparkle.example.com', 'agents', '087' -> 'did:web:sofia.sparkle.example.com:agents:087'"""
    did = "did:web:" + host.replace(":", "%3A")
    for seg in path_segments:
        did += ":" + seg
    return did


def did_to_resolution_url(did: str) -> str:
    """Inverse of domain_and_path_to_did — the URL a resolver must GET."""
    assert did.startswith("did:web:")
    parts = did[len("did:web:"):].split(":")
    host = parts[0].replace("%3A", ":")
    path_segments = parts[1:]
    if path_segments:
        return f"https://{host}/{'/'.join(path_segments)}/did.json"
    return f"https://{host}/.well-known/did.json"


def bootstrap_signing_key(
    agent_id: str, keystore: "IdentityKeystoreBase"
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey, bool]:
    """Load the agent's existing signing key from the keystore, or
    generate and persist a new one on first bootstrap.

    Returns (private_key, public_key, created) — `created` tells the
    caller whether this is a brand-new identity (should publish a fresh
    DID Document) or a restart of an existing one (should keep serving
    the DID Document already on record, unchanged)."""
    private_key, created = keystore.get_or_create(agent_id)
    return private_key, private_key.public_key(), created


def build_did_document(
    *,
    host: str,
    agent_path_segments: list[str],
    governing_authority_did: str,
    signing_public_key: Ed25519PublicKey,
    now: dt.datetime | None = None,
) -> DIDDocument:
    """
    Build (but do not yet publish) the DID Document for one agent.

    Two different `controller` fields, two different meanings — this was
    wrong in an earlier draft, corrected per the W185 alignment review:

        DIDDocument.controller        -> governance of the DID as a whole
                                          (the Governing Authority: can
                                          deactivate/rotate in an emergency)
        VerificationMethod.controller -> who operates THIS key day-to-day
                                          (the agent itself — it holds and
                                          uses its own signing key; the
                                          Authority does not sign on its
                                          behalf)

    Setting both to governing_authority_did (the earlier bug) implied the
    Authority co-signs every credential the agent issues, which is not
    the model: "centralized trust, decentralized identity format" means
    the Authority anchors *who counts as legitimate*, not that it
    operates every agent's key.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    did = domain_and_path_to_did(host, *agent_path_segments)
    key_id = f"{did}#key-1"

    raw_pub = signing_public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    vm = VerificationMethod(
        id=key_id,
        controller=did,  # the agent operates its own key — not the Authority
        public_key_multibase=_multibase_encode_ed25519_pub(raw_pub),
    )

    return DIDDocument(
        id=did,
        controller=governing_authority_did,  # governance of the DID stays with the Authority
        verification_method=[vm],
        authentication=[key_id],
        assertion_method=[key_id],
        created=now,
    )