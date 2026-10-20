"""
Verifiable Presentation — end-to-end.

Covers:
    - identity presentation (una sola credenziale) e delegated
      presentation (Operational + Identity Credential del Grantor)
    - il binding holder: solo chi controlla la chiave del holder può
      firmare una presentazione valida
    - protezione replay via challenge
    - tamper check sul contenuto della presentazione
    - protezione strutturale su proof_purpose sbagliato
    - unpack_presentation con forme non valide
    - il flusso completo, end-to-end, con IdentityManager + DelegationManager
      reali (stesso scenario "Sparkle Grantor autorizza un agente di B"
      usato nel deck)
"""

import datetime as dt
import uuid

import pytest

from stls_identity import (
    CredentialVerificationError,
    DelegationManager,
    IdentityManager,
    InMemoryIdentityKeystore,
    Sentinel,
    bootstrap_signing_key,
    build_did_document,
    build_verifiable_presentation,
)


class _InMemoryStatusListManager:
    def __init__(self):
        self._next = 0
        self._revoked = set()

    async def allocate_index(self):
        idx = self._next
        self._next += 1
        return idx

    async def revoke(self, index):
        self._revoked.add(index)

    async def is_revoked(self, index):
        return index in self._revoked


async def _noop_audit_hook(entry):
    pass


class _Scenario:
    """Authority Sparkle -> Grantor 099 (Sparkle) -> Requestor 042 (B),
    stesso scenario usato per gli esempi nel deck."""

    async def setup(self):
        self.keystore = InMemoryIdentityKeystore()

        self.authority_did = "did:web:sofia.sparkle.example.com"
        self.authority_key, self.authority_pub, _ = bootstrap_signing_key(
            "sparkle-governing-authority", self.keystore
        )
        self.authority_vm_id = f"{self.authority_did}#key-1"

        self.grantor_key, self.grantor_pub, _ = bootstrap_signing_key(
            "sparkle-agent-099", self.keystore
        )
        self.grantor_doc = build_did_document(
            host="sofia.sparkle.example.com", agent_path_segments=["agents", "099"],
            governing_authority_did=self.authority_did, signing_public_key=self.grantor_pub,
        )
        self.grantor_vm_id = f"{self.grantor_doc.id}#key-1"

        self.requestor_key, self.requestor_pub, _ = bootstrap_signing_key("b-agent-042", self.keystore)
        self.requestor_doc = build_did_document(
            host="b.example.com", agent_path_segments=["agents", "042"],
            governing_authority_did="did:web:b.example.com", signing_public_key=self.requestor_pub,
        )
        self.requestor_vm_id = f"{self.requestor_doc.id}#key-1"

        self.issuer = IdentityManager(
            governing_authority_did=self.authority_did, signing_key=self.authority_key,
            signing_verification_method_id=self.authority_vm_id,
            status_list_manager=_InMemoryStatusListManager(),
            status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
            audit_hook=_noop_audit_hook,
        )
        self.grantor_identity_credential = await self.issuer.issue_identity_credential(
            agent_did=self.grantor_doc.id, client_id="sparkle-agent-099",
            roles=["grantor"],
        )

        self.delegation = DelegationManager(
            grantor_did=self.grantor_doc.id, grantor_signing_key=self.grantor_key,
            grantor_verification_method_id=self.grantor_vm_id,
            status_list_manager=_InMemoryStatusListManager(),
            status_list_publish_url="https://sofia.sparkle.example.com/agents/099/status-list",
            audit_hook=_noop_audit_hook,
        )
        self.operational_credential = await self.delegation.issue_operational_credential(
            grantor_identity_credential=self.grantor_identity_credential,
            requestor_did=self.requestor_doc.id, requestor_client_id="b-agent-042",
            scope={"action": "invoke-workflow", "resource": "sparkle-agent-007"},
        )

        self.sentinel = Sentinel(trusted_issuer_keys={
            self.authority_vm_id: self.authority_pub,
            self.grantor_vm_id: self.grantor_pub,
            self.requestor_vm_id: self.requestor_pub,  # per verificare il proof della VP (holder)
        })
        return self


async def _scenario() -> _Scenario:
    return await _Scenario().setup()


# ---------------------------------------------------------------------------
# Identity presentation — una sola credenziale
# ---------------------------------------------------------------------------

async def test_identity_presentation_roundtrip():
    s = await _scenario()
    challenge = str(uuid.uuid4())

    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id,
        holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential],
        challenge=challenge,
    )

    assert vp.holder == s.grantor_doc.id
    assert vp.proof.proof_purpose == "authentication"

    s.sentinel.verify_presentation_proof(vp, expected_challenge=challenge)
    (identity,) = Sentinel.unpack_presentation(vp)
    assert identity is s.grantor_identity_credential or identity.id == s.grantor_identity_credential.id

    now = dt.datetime.now(dt.timezone.utc)
    await s.sentinel.verify(identity, status_bitstring_fetch_time=now, is_revoked=False)


# ---------------------------------------------------------------------------
# Delegated presentation — Operational + Identity Credential del Grantor
# ---------------------------------------------------------------------------

async def test_delegated_presentation_roundtrip_and_full_chain():
    s = await _scenario()
    challenge = str(uuid.uuid4())

    # Il Requestor (042) presenta le proprie credenziali nell'ordine che vuole
    vp = build_verifiable_presentation(
        holder_did=s.requestor_doc.id,
        holder_signing_key=s.requestor_key,
        holder_verification_method_id=s.requestor_vm_id,
        credentials=[s.grantor_identity_credential, s.operational_credential],  # ordine "sbagliato" apposta
        challenge=challenge,
    )

    s.sentinel.verify_presentation_proof(vp, expected_challenge=challenge)
    operational, identity = Sentinel.unpack_presentation(vp)

    # unpack_presentation deve restituire l'ordine corretto indipendentemente
    # da come il Requestor le ha messe nella lista
    assert operational.id == s.operational_credential.id
    assert identity.id == s.grantor_identity_credential.id

    now = dt.datetime.now(dt.timezone.utc)
    await s.sentinel.verify_delegation_chain(
        operational,
        grantor_identity_credential=identity,
        grantor_identity_fetch_time=now, grantor_identity_is_revoked=False,
        operational_fetch_time=now, operational_is_revoked=False,
    )


# ---------------------------------------------------------------------------
# Holder binding — solo chi ha la chiave del holder può firmare
# ---------------------------------------------------------------------------

async def test_presentation_signed_by_wrong_key_fails_verification():
    s = await _scenario()
    challenge = str(uuid.uuid4())

    # Il Requestor dichiara di essere 099 (il Grantor) ma firma con la
    # propria chiave (042) — furto di identità nella presentazione.
    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id,             # dichiara di essere il Grantor...
        holder_signing_key=s.requestor_key,       # ...ma firma con la chiave sbagliata
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential],
        challenge=challenge,
    )

    with pytest.raises(CredentialVerificationError, match="signature"):
        s.sentinel.verify_presentation_proof(vp, expected_challenge=challenge)


async def test_presentation_from_unknown_holder_is_rejected():
    s = await _scenario()
    challenge = str(uuid.uuid4())

    unknown_key, unknown_pub, _ = bootstrap_signing_key("mystery-agent", s.keystore)
    vp = build_verifiable_presentation(
        holder_did="did:web:unknown.example.com:agents:999",
        holder_signing_key=unknown_key,
        holder_verification_method_id="did:web:unknown.example.com:agents:999#key-1",
        credentials=[s.grantor_identity_credential],
        challenge=challenge,
    )

    with pytest.raises(CredentialVerificationError, match="unknown or untrusted"):
        s.sentinel.verify_presentation_proof(vp, expected_challenge=challenge)


# ---------------------------------------------------------------------------
# Replay protection — il challenge deve corrispondere esattamente
# ---------------------------------------------------------------------------

async def test_presentation_replay_with_stale_challenge_is_rejected():
    s = await _scenario()
    original_challenge = str(uuid.uuid4())

    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id, holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential], challenge=original_challenge,
    )

    # Un attacker cattura questa VP e prova a riusarla su una richiesta
    # successiva, che ha emesso un challenge diverso.
    new_challenge = str(uuid.uuid4())
    with pytest.raises(CredentialVerificationError, match="replay"):
        s.sentinel.verify_presentation_proof(vp, expected_challenge=new_challenge)


# ---------------------------------------------------------------------------
# Tamper check — modificare il contenuto dopo la firma deve invalidarla
# ---------------------------------------------------------------------------

async def test_tampering_with_credential_list_after_signing_is_detected():
    s = await _scenario()
    challenge = str(uuid.uuid4())

    vp = build_verifiable_presentation(
        holder_did=s.requestor_doc.id, holder_signing_key=s.requestor_key,
        holder_verification_method_id=s.requestor_vm_id,
        credentials=[s.operational_credential, s.grantor_identity_credential],
        challenge=challenge,
    )

    # Un attacker prova a scambiare la Operational Credential con una a
    # scope più ampio, lasciando intatta la firma della presentazione.
    tampered = vp.model_copy(deep=True)
    tampered.verifiable_credential[0].credential_subject.scope["resource"] = "admin-panel"

    with pytest.raises(CredentialVerificationError, match="signature"):
        s.sentinel.verify_presentation_proof(tampered, expected_challenge=challenge)


# ---------------------------------------------------------------------------
# proof_purpose sbagliato — protezione strutturale, non solo di convenzione
# ---------------------------------------------------------------------------

async def test_presentation_with_wrong_proof_purpose_is_rejected():
    s = await _scenario()
    challenge = str(uuid.uuid4())

    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id, holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential], challenge=challenge,
    )

    # Simula un'implementazione non conforme che manda un proof di tipo
    # "assertionMethod" (quello di una credenziale) invece che
    # "authentication" (quello di una presentazione). Il modello Pydantic
    # non lo permetterebbe in costruzione normale (Literal) — qui bypassiamo
    # con un'assegnazione diretta per simulare un payload esterno malformato.
    vp.proof.proof_purpose = "assertionMethod"

    with pytest.raises(CredentialVerificationError, match="wrong purpose"):
        s.sentinel.verify_presentation_proof(vp, expected_challenge=challenge)


# ---------------------------------------------------------------------------
# unpack_presentation — forme non valide
# ---------------------------------------------------------------------------

async def test_unpack_presentation_rejects_two_identity_credentials():
    s = await _scenario()
    other_key, other_pub, _ = bootstrap_signing_key("another-agent", s.keystore)
    other_doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "123"],
        governing_authority_did=s.authority_did, signing_public_key=other_pub,
    )
    other_identity = await s.issuer.issue_identity_credential(
        agent_did=other_doc.id, client_id="agent-123",
    )

    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id, holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential, other_identity],
        challenge=str(uuid.uuid4()),
    )

    with pytest.raises(CredentialVerificationError, match="does not match a known shape"):
        Sentinel.unpack_presentation(vp)


async def test_unpack_presentation_rejects_empty_list():
    with pytest.raises(Exception):
        # una VerifiablePresentation senza credenziali non è nemmeno
        # costruibile in modo sensato per questo flusso — il builder non
        # impedisce esplicitamente la lista vuota, ma unpack_presentation
        # deve comunque rifiutarla invece di restituire qualcosa di ambiguo.
        from stls_identity import VerifiablePresentation
        vp = VerifiablePresentation(holder="did:web:example.com:agents:1", verifiable_credential=[])
        Sentinel.unpack_presentation(vp)