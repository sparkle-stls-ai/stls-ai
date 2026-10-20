"""
stls_identity.models
=====================

Pydantic v2 data models for the STLS identity layer: DID Documents and
Verifiable Credentials (W3C-aligned, camelCase on the wire — same
alias_generator convention already used in stls_a2a_card_model.py).

Scope of this module: Phase 1-3.
    Phase 1 — minimal DID, no credentials yet.
    Phase 2 — NetworkAuthorityCredential, issued alongside (not replacing)
              the existing API key.
    Phase 3 — OperationalCredential: Grantor -> Requestor delegation,
              depth-limited to 1 by construction (see delegation.py's
              module docstring for how the limit is enforced).

Explicitly OUT of scope here (later phases, not implemented in this file):
    - Cross-federation trust anchor resolution (Phase 4+)
    - Selective disclosure / ZKP (Phase 5)

IMPORTANT — key material:
    A DID's verificationMethod needs a *signing* key. X25519 (used
    everywhere else in stls-core for the PQC/Arqit key exchange) is a
    Diffie-Hellman curve — it cannot sign anything. stls-identity
    therefore provisions a *separate* Ed25519 keypair per agent at
    bootstrap, stored alongside the existing X25519 material in the same
    keystore. The two keys serve different purposes and are never
    interchangeable:

        X25519  -> session key agreement (stls-core KeyProvider, unchanged)
        Ed25519 -> signs DID Documents and Verifiable Credentials (new)
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_MODEL_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    extra="forbid",
)


# ---------------------------------------------------------------------------
# DID Document (did:web)
# ---------------------------------------------------------------------------

class VerificationMethod(BaseModel):
    """A single Ed25519 verification key published in the DID Document."""

    model_config = _MODEL_CONFIG

    id: str  # e.g. "did:web:sofia.sparkle.example.com:agents:087#key-1"
    type: Literal["Ed25519VerificationKey2020"] = "Ed25519VerificationKey2020"
    controller: str  # the Governing Authority DID
    public_key_multibase: str  # multibase-encoded raw Ed25519 public key


class DIDDocument(BaseModel):
    """
    Minimal did:web document.

    Served at:  https://{host}/.well-known/did.json                 (root agent)
                https://{host}/agents/{agent_id}/did.json            (per-agent)
    which resolves 1:1 from the DID string via the did:web method spec.
    """

    model_config = _MODEL_CONFIG

    context: list[str] = Field(
        default_factory=lambda: [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/ed25519-2020/v1",
        ],
        alias="@context",
    )
    id: str  # e.g. "did:web:sofia.sparkle.example.com:agents:087"
    controller: str  # Governing Authority DID — centralized control, per W185 §-style model
    verification_method: list[VerificationMethod]
    authentication: list[str]  # list of verification_method ids usable for auth
    assertion_method: list[str]  # list of verification_method ids usable to sign VCs
    created: dt.datetime


# ---------------------------------------------------------------------------
# Verifiable Credential — status list entry (revocation pointer)
# ---------------------------------------------------------------------------

class CredentialStatus(BaseModel):
    """
    Pointer into the issuer's BitstringStatusList (see status.py).
    Presence of this block is what makes a VC revocable without
    re-contacting the issuer for every verification.
    """

    model_config = _MODEL_CONFIG

    id: str  # URL of the status list credential + "#{status_list_index}"
    type: Literal["BitstringStatusListEntry"] = "BitstringStatusListEntry"
    status_purpose: Literal["revocation", "suspension"] = "revocation"
    status_list_index: int
    status_list_credential: str  # URL where the bitstring document is published


# ---------------------------------------------------------------------------
# Verifiable Credential — base + Phase 2 subtype
# ---------------------------------------------------------------------------

class KeyStorageTier(str, Enum):
    """
    Declares HOW an agent's private key is protected — not which
    algorithm it uses (that's cryptoProfile, a separate concern for a
    later PQC pass). W185 alignment field.

    Every IdentityKeystoreBase backend today (InMemoryIdentityKeystore,
    RedisIdentityKeystore) is software-only key storage — honestly
    TIER_1_SOFTWARE across the board, regardless of which one is
    selected. Only a genuinely hardware-backed keystore (TPM/TEE, or an
    HSM where the raw key material never leaves the device) would
    justify a higher tier. Do not set TIER_2/TIER_3 based on which
    *backend* is configured — set it based on what actually protects
    the key.
    """

    tier_1_software = "TIER_1_SOFTWARE"
    tier_2_tpm = "TIER_2_TPM"
    tier_3_hsm = "TIER_3_HSM"


class CredentialSubject(BaseModel):
    """What the credential asserts about the agent. Kept intentionally
    small in Phase 2 — this is an identity claim, not a scoped grant."""

    model_config = _MODEL_CONFIG

    id: str  # the agent's DID
    client_id: str  # bridges to the existing KeyRecord.client_id, for the
                     # transition period where both systems run in parallel

    principal_ref: str | None = None
    """Opaque reference to the human/org principal behind this agent
    (e.g. "org:sparkle:bda"). Accountability metadata — the Sentinel
    does not interpret it, same posture as OperationalCredentialSubject.scope.
    W185 alignment field."""

    key_storage_tier: KeyStorageTier = KeyStorageTier.tier_1_software
    """See KeyStorageTier's docstring. Defaults to the honest current
    state of every backend this codebase ships today."""

    roles: list[str] = Field(default_factory=list)
    """Coarse-grained role declaration (e.g. ["grantor"]) — NOT a
    fine-grained capability list. This is the STLS answer to W185's
    RoleCredential proposal: rather than a separate credential type
    with its own status list per role, one identity credential declares
    which roles it may assume, and DelegationManager checks this before
    letting an agent act as Grantor (see delegation.py). Deliberately
    does NOT carry per-operation scope — that stays on
    OperationalCredentialSubject.scope, where Identity/Authorization
    separation says it belongs."""


class VerifiableCredential(BaseModel):
    """
    Generic VC envelope. NetworkAuthorityCredential (below) is the only
    concrete credential type issued in this Phase 1-2 scaffold.
    """

    model_config = _MODEL_CONFIG

    context: list[str] = Field(
        default_factory=lambda: [
            "https://www.w3.org/ns/credentials/v2",
        ],
        alias="@context",
    )
    id: str  # urn:uuid:... — unique credential id, server-generated (mirrors
             # session_id being server-controlled in the existing handshake)
    type: list[str] = Field(default_factory=lambda: ["VerifiableCredential"])
    schema_version: str = "1.0"
    """Version of THIS credential schema as implemented by stls-identity
    (bump when the shape changes) — not W185's own spec version. W185
    alignment field, applies to every credential type via this base
    class (NetworkAuthorityCredential and OperationalCredential alike)."""
    issuer: str  # Governing Authority DID
    valid_from: dt.datetime
    valid_until: dt.datetime | None = None
    credential_subject: CredentialSubject
    credential_status: CredentialStatus
    proof: "CredentialProof | None" = None  # absent until signed


class NetworkAuthorityCredential(VerifiableCredential):
    """
    Phase 2 credential: asserts *identity*, not authorization scope.
    This is the DID/VC analogue of today's KeyRecord — issued alongside
    it, not replacing it, during the whole migration window.
    """

    type: list[str] = Field(
        default_factory=lambda: ["VerifiableCredential", "NetworkAuthorityCredential"]
    )


# ---------------------------------------------------------------------------
# Verifiable Credential — Phase 3: Operational Credential (delegation)
# ---------------------------------------------------------------------------

class OperationalCredentialSubject(BaseModel):
    """
    What the Grantor is authorizing the Requestor to do — a scoped grant,
    not a re-assertion of identity (that's what NetworkAuthorityCredential
    is for; a Requestor still needs its own identity credential separately,
    this only carries the permission).

    `scope` is intentionally an open dict rather than a fixed schema: the
    concrete shape is domain-specific (e.g. for the eLOA cross-connect
    use case: {"action": "cross-connect-activate", "facility_code": "MI1",
    "port_id": "42"}). Sentinel does not interpret `scope` — the
    resource-owning service that receives the presented credential does,
    the same way endpoints/scopes on today's KeyRecord are interpreted
    by the endpoint itself, not by ApiKeyManager.
    """

    model_config = _MODEL_CONFIG

    id: str  # the Requestor's DID
    client_id: str  # bridges to KeyRecord.client_id, same as CredentialSubject
    scope: dict[str, Any] = Field(default_factory=dict)


class OperationalCredential(VerifiableCredential):
    """
    Phase 3 credential: Grantor -> Requestor delegation.

    `issuer` here is the GRANTOR's own DID — deliberately NOT required to
    be the Governing Authority. Any agent holding a valid
    NetworkAuthorityCredential can act as Grantor; see delegation.py for
    how DelegationManager enforces that precondition and why that
    requirement is what keeps delegation depth at exactly 1 (a Requestor
    holding only an OperationalCredential has no NetworkAuthorityCredential
    of its own to grant further with — depth-limiting by construction,
    not by a counter that could be tampered with).
    """

    type: list[str] = Field(
        default_factory=lambda: ["VerifiableCredential", "OperationalCredential"]
    )
    credential_subject: OperationalCredentialSubject
    delegation_depth: Literal[1] = 1
    """Fixed at 1 — documents the constraint rather than enforcing it by
    itself (the enforcement is structural, see class docstring). A
    verifier that sees any value other than 1 here knows the credential
    doesn't conform to this system's delegation model at all."""


class CredentialProof(BaseModel):
    """
    Ed25519 detached signature over the RFC 8785 (JCS) canonicalized
    credential.

    W185 alignment fix: the proof type is `DataIntegrityProof` with
    `cryptosuite: eddsa-jcs-2022`, not `Ed25519Signature2020`.
    Ed25519Signature2020 (as W3C specifies it) implies RDF Dataset
    Canonicalization (URDNA2015) — a different, heavier canonicalization
    than what this codebase actually does. `eddsa-jcs-2022` is the
    correctly-named cryptosuite for "Ed25519 over JCS-canonicalized
    JSON" — same crypto, correct label. See rfc8785.dumps() call sites
    in issuer.py / delegation.py / presentation.py / verifier.py.
    """

    model_config = _MODEL_CONFIG

    type: Literal["DataIntegrityProof"] = "DataIntegrityProof"
    cryptosuite: Literal["eddsa-jcs-2022"] = "eddsa-jcs-2022"
    created: dt.datetime
    verification_method: str  # id of the VerificationMethod that signed this
    proof_purpose: Literal["assertionMethod"] = "assertionMethod"
    proof_value: str  # multibase-encoded raw signature bytes


# ---------------------------------------------------------------------------
# Status list document (published, resolved by any verifier)
# ---------------------------------------------------------------------------

class StatusPurpose(str, Enum):
    revocation = "revocation"
    suspension = "suspension"


class StatusListCredential(BaseModel):
    """
    The published bitstring itself. One document covers every credential
    issued by this Governing Authority — see status.py for how the
    bitstring is built and kept fresh against the 5-minute MRI.
    """

    model_config = _MODEL_CONFIG

    context: list[str] = Field(
        default_factory=lambda: ["https://www.w3.org/ns/credentials/v2"],
        alias="@context",
    )
    id: str  # published URL of this document
    type: list[str] = Field(
        default_factory=lambda: ["VerifiableCredential", "BitstringStatusListCredential"]
    )
    issuer: str
    valid_from: dt.datetime
    status_purpose: StatusPurpose = StatusPurpose.revocation
    encoded_list: str  # base64url of the GZIP-compressed bitstring
    proof: CredentialProof | None = None


# ---------------------------------------------------------------------------
# Verifiable Presentation — the envelope that actually travels with a
# request (see "Cosa Viaggia, Cosa È Pubblicato" in the deck)
# ---------------------------------------------------------------------------

class PresentationProof(BaseModel):
    """
    Same shape as CredentialProof, but deliberately a DISTINCT type —
    proofPurpose is always "authentication" here (proving control of the
    holder DID), never "assertionMethod" (asserting a claim, what
    CredentialProof is for). A verifier must be able to tell the two
    apart structurally, not just by convention, so they are not
    type-interchangeable even though the signing mechanism is identical.
    """

    model_config = _MODEL_CONFIG

    type: Literal["DataIntegrityProof"] = "DataIntegrityProof"
    cryptosuite: Literal["eddsa-jcs-2022"] = "eddsa-jcs-2022"
    created: dt.datetime
    verification_method: str
    proof_purpose: Literal["authentication"] = "authentication"
    challenge: str
    """Nonce supplied by the verifier for THIS request. Prevents a
    captured VP from being replayed on a later, different request —
    same role as the nonce in stls_core_handshake.HandshakeRequest."""
    proof_value: str


class VerifiablePresentation(BaseModel):
    """
    Wraps one or more Verifiable Credentials in a single signed envelope
    with holder binding — the object that actually travels with an A2A
    request, per the "Cosa Viaggia, Cosa È Pubblicato" slide.

    For a plain identity call: verifiable_credential holds exactly one
    NetworkAuthorityCredential.

    For a delegated call: it holds exactly two — the OperationalCredential
    being presented AND the Grantor's own NetworkAuthorityCredential.
    Both are required for Sentinel.verify_delegation_chain() to run at
    all (see delegation.py's module docstring) — this is the "bundle"
    that was previously an open question, now given a concrete shape.

    `proof` here is NOT a credential's proof: it signs over the
    presentation itself (holder + the credential list + a fresh
    challenge), proving that whoever is presenting this actually
    controls the `holder` DID. A bare VC never proves that on its own —
    anyone who captured it could otherwise replay it. Same
    challenge/proof pattern already in production in
    stls_core_handshake.perform_handshake() (server nonce, HMAC proof
    over it) — here asymmetric (Ed25519) instead of the shared-secret
    HMAC used there, because there is no shared secret between
    federated platforms.
    """

    model_config = _MODEL_CONFIG

    context: list[str] = Field(
        default_factory=lambda: ["https://www.w3.org/ns/credentials/v2"],
        alias="@context",
    )
    type: list[str] = Field(default_factory=lambda: ["VerifiablePresentation"])
    holder: str
    """DID of the agent presenting this VP — must be the same DID whose
    key signs `proof` below (holder binding), and for a delegated call
    must equal the OperationalCredential's credentialSubject.id (the
    Requestor) — checked by the verifier, not enforced by this model."""
    verifiable_credential: list[NetworkAuthorityCredential | OperationalCredential]
    proof: PresentationProof | None = None


# ---------------------------------------------------------------------------
# Trust Anchor Registry — Fase 2. Minimal by design: authority_id + DID +
# current public key + onboarding metadata. Nothing about individual
# agents or credentials lives here — see "Chi Possiede Cosa" in the deck.
# ---------------------------------------------------------------------------

class TrustAnchorEntry(BaseModel):
    """
    One key generation for one recognized Governing Authority. An
    Authority can have MULTIPLE entries over time (one per key
    rotation) — the registry is a list of these, deliberately tiny
    (tens to low hundreds of *authorities*, a handful of entries per
    authority over its lifetime), because it only tracks *who counts as
    legitimate and with which key, when*, never individual agents,
    credentials, or per-credential revocation state (those stay
    per-issuer, resolved via each platform's own bridge).

    Key history exists to close a real gap: if an Authority rotates its
    key, a credential it signed BEFORE the rotation, still inside its
    own validity window, must remain verifiable — the old key must stay
    resolvable for exactly that purpose, even though it's no longer the
    key trusted for anything new. See TrustAnchorRegistryStoreBase's
    module docstring for how the store models this.
    """

    model_config = _MODEL_CONFIG

    authority_id: str
    """Short human-assigned identifier for onboarding/admin purposes
    (e.g. "sparkle", "operatore-b") — NOT used for any cryptographic
    check, purely operational bookkeeping. Shared across every key
    generation of the same Authority."""

    did: str
    """The Governing Authority's own DID (e.g. did:web:sofia.sparkle.example.com)."""

    verification_method_id: str
    """The key id this entry trusts (e.g. did:web:sofia.sparkle.example.com#key-1)
    — matches CredentialProof.verification_method on credentials signed
    with THIS key generation. Part of the composite identity of a row
    together with authority_id: the same Authority has a different
    verification_method_id per rotation."""

    public_key_multibase: str

    onboarded_at: dt.datetime
    """When THIS key generation started being trusted — the original
    onboarding date for an Authority's first entry, or the rotation
    timestamp for every entry after."""

    valid_until: dt.datetime | None = None
    """None = this is the CURRENTLY active key for this Authority. Set
    (to the rotation timestamp) when a newer key generation replaces
    this one — the row is kept, not deleted, specifically so a
    credential signed before this timestamp still resolves against it."""

    revoked_at: dt.datetime | None = None
    """Emergency revocation of THIS SPECIFIC key generation (e.g.
    suspected compromise) — stronger than a normal rotation
    (valid_until): a revoked key must be rejected even for verifying
    credentials signed while it was nominally still valid. Distinct
    from offboarding the whole Authority (see
    TrustAnchorRegistryStoreBase.revoke_authority), which revokes every
    key generation at once."""


CredentialSubject.model_rebuild()
VerifiableCredential.model_rebuild()
OperationalCredentialSubject.model_rebuild()
OperationalCredential.model_rebuild()
VerifiablePresentation.model_rebuild()