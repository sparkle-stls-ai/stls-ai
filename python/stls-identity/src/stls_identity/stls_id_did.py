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

from typing import TYPE_CHECKING, Awaitable, Callable

from .stls_id_models import DIDDocument, VerificationMethod

if TYPE_CHECKING:
    from .stls_id_keystore import IdentityKeystoreBase
    from .stls_id_postgres_store import DIDDocumentStoreBase

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
        valid_from=now,
    )

    return DIDDocument(
        id=did,
        controller=governing_authority_did,  # governance of the DID stays with the Authority
        verification_method=[vm],
        authentication=[key_id],
        assertion_method=[key_id],
        created=now,
    )


def current_verification_method(document: DIDDocument) -> VerificationMethod:
    """
    The verification method an agent's own key rotation (issue #42)
    makes it unsafe to find by position: after a rotation,
    document.verification_method holds every key generation ever
    issued, oldest first, so verification_method[0] is the OLDEST key,
    not the current one. This is the replacement — resolves "current"
    the same way TrustAnchorRegistryStoreBase.get() does: valid_until
    IS NULL (and not revoked).

    Raises:
        ValueError: zero or more than one currently-active entry — a
            document that was hand-edited or corrupted outside
            rotate_agent_signing_key(), which always leaves exactly
            one current entry.
    """
    current = [
        vm for vm in document.verification_method
        if vm.valid_until is None and vm.revoked_at is None
    ]
    if len(current) != 1:
        raise ValueError(
            f"{document.id}: expected exactly one currently-active verification "
            f"method, found {len(current)}"
        )
    return current[0]


def resolve_holder_key_at(
    document: DIDDocument, verification_method_id: str, at: dt.datetime,
) -> Ed25519PublicKey | None:
    """
    The per-agent analogue of PostgresTrustAnchorRegistryStore's
    get_key_valid_at() / get_key_valid_at_by_verification_method() —
    was this key generation the one valid for this agent's DID
    Document at time `at`? Unlike the Trust Anchor Registry, there's no
    separate store to query: everything needed is already in the one
    DIDDocument fetched via DIDDocumentStoreBase.get(), since an
    agent's key history lives IN its own document (issue #42), not in
    a side table.

    Callers should pass the signature's OWN signing time
    (CredentialProof.created / PresentationProof.created), not "now" —
    same rule as TrustAnchorRegistryClient.resolve_historical().
    """
    for vm in document.verification_method:
        if vm.id != verification_method_id:
            continue
        if vm.valid_from is not None and at < vm.valid_from:
            continue
        if vm.valid_until is not None and at >= vm.valid_until:
            continue
        if vm.revoked_at is not None and at >= vm.revoked_at:
            continue
        return decode_multibase_ed25519_public_key(vm.public_key_multibase)
    return None


def agent_id_from_verification_method_id(verification_method_id: str, host: str) -> str | None:
    """
    Inverse of domain_and_path_to_did(host, "agents", agent_id) + "#key-N"
    — extracts the agent_id segment from one of ITS OWN verification
    method ids, so a resolver that only has the id string (e.g.
    CredentialProof.verification_method / PresentationProof's own) can
    find which agent's DIDDocument to fetch. Returns None if
    verification_method_id doesn't belong to an agent DID under this
    host (e.g. it's the Governing Authority's own key instead).
    """
    did = verification_method_id.split("#", 1)[0]
    prefix = domain_and_path_to_did(host, "agents") + ":"
    if not did.startswith(prefix) or did == prefix:
        return None
    return did[len(prefix):]


def rotate_agent_signing_key(
    document: DIDDocument,
    *,
    new_signing_public_key: Ed25519PublicKey,
    now: dt.datetime | None = None,
) -> DIDDocument:
    """
    Rotates an agent's own signing key WITHIN its existing DIDDocument —
    the per-agent analogue of TrustAnchorRegistryStoreBase.put()'s
    "close the previous key generation, open a new one" rotation path
    (issue #42), scoped to a single agent instead of the shared Trust
    Anchor Registry. Pure/in-memory: the caller still has to persist
    the result via DIDDocumentStoreBase.put() (see rotate_signing_key()
    below for the full orchestration, including the private-key half).

    Keeps every prior key generation in verification_method (audit
    trail + historical resolution via resolve_holder_key_at()) but
    repoints authentication/assertion_method at ONLY the new key —
    those two arrays declare which key currently authenticates/signs,
    never history.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    old_vm = current_verification_method(document)

    next_n = len(document.verification_method) + 1
    new_key_id = f"{document.id}#key-{next_n}"
    raw_pub = new_signing_public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    new_vm = VerificationMethod(
        id=new_key_id,
        controller=document.id,
        public_key_multibase=_multibase_encode_ed25519_pub(raw_pub),
        valid_from=now,
    )
    closed_old_vm = old_vm.model_copy(update={"valid_until": now})

    updated_methods = [
        closed_old_vm if vm.id == old_vm.id else vm
        for vm in document.verification_method
    ] + [new_vm]

    return document.model_copy(update={
        "verification_method": updated_methods,
        "authentication": [new_key_id],
        "assertion_method": [new_key_id],
    })


async def rotate_signing_key(
    agent_id: str,
    keystore: "IdentityKeystoreBase",
    did_store: "DIDDocumentStoreBase",
    *,
    now: dt.datetime | None = None,
) -> tuple[Ed25519PrivateKey, DIDDocument]:
    """
    Full key rotation for an already-bootstrapped agent (issue #42):
    generates a new private key (IdentityKeystoreBase.rotate(), which
    unconditionally overwrites — the old private key is never needed
    again, only its PUBLIC half, which stays resolvable via the
    updated DIDDocument), then updates and persists the agent's
    DIDDocument so the OLD key remains resolvable for signatures made
    before this call (resolve_holder_key_at()).

    Raises:
        ValueError: this agent has no DID Document yet — this rotates
            an EXISTING identity, it does not bootstrap a new one (see
            bootstrap_signing_key()/build_did_document() for that).
    """
    document = await did_store.get(agent_id)
    if document is None:
        raise ValueError(
            f"{agent_id}: no DID Document on record — bootstrap this agent's "
            "identity before rotating its key"
        )

    new_key = keystore.rotate(agent_id)
    rotated_document = rotate_agent_signing_key(
        document, new_signing_public_key=new_key.public_key(), now=now,
    )
    await did_store.put(agent_id, rotated_document)
    return new_key, rotated_document


def make_did_document_holder_key_resolver(
    did_store: "DIDDocumentStoreBase",
    *,
    governing_authority_host: str,
) -> Callable[[str, dt.datetime], Awaitable[Ed25519PublicKey | None]]:
    """
    Builds the async callable Sentinel's `holder_key_resolver` expects
    (stls_id_verifier.py) — the holder-key equivalent of
    TrustAnchorRegistryClient.resolve_historical(), but backed by each
    agent's own DIDDocument instead of a shared registry (issue #42):
    `verify_presentation_proof()`'s historical fallback was explicitly
    NOT wired to anything before this, since the Trust Anchor Registry
    never tracked individual agent keys (see Sentinel's own docstring).

    Deliberately a closure over did_store rather than a new class: no
    caching is needed here (unlike TrustAnchorRegistryClient, which
    exists specifically to avoid a DB round trip on the hot path) —
    this is already the slow, rare fallback path, same posture as
    resolve_historical() itself.
    """
    async def _resolve(verification_method_id: str, at: dt.datetime) -> Ed25519PublicKey | None:
        agent_id = agent_id_from_verification_method_id(
            verification_method_id, governing_authority_host,
        )
        if agent_id is None:
            return None
        document = await did_store.get(agent_id)
        if document is None:
            return None
        return resolve_holder_key_at(document, verification_method_id, at)

    return _resolve