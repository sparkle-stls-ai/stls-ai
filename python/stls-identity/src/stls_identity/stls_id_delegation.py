"""
stls_identity.delegation
==========================

DelegationManager — lets an agent that already holds a valid
NetworkAuthorityCredential act as a Grantor, issuing scoped
OperationalCredentials to a Requestor agent.

How the depth-1 limit is actually enforced
-------------------------------------------
Not by a counter (a counter can be tampered with, or simply omitted by a
non-compliant implementation on the other side of a federation). It's
enforced structurally, by what DelegationManager requires to construct
one at all:

    to issue an OperationalCredential, the caller must supply the
    Grantor's own, currently-valid NetworkAuthorityCredential.

A Requestor who has only ever received an OperationalCredential has no
NetworkAuthorityCredential of their own — DelegationManager has nothing
to accept from them, so they structurally cannot become a Grantor for
further delegation using that credential. To re-delegate, an agent would
need to separately hold an identity credential issued directly by a
Governing Authority — which makes it a first-class Grantor in its own
right, not a re-delegation of the original grant. There is no code path
in this module that lets an OperationalCredential itself justify issuing
another OperationalCredential.

W185 alignment addition — role check, a SEPARATE gate from depth-1:
    Holding *any* valid NetworkAuthorityCredential used to be sufficient
    to act as Grantor. That conflated "has a legitimate identity" with
    "is authorized to delegate" — two different questions. Now the
    Grantor's own identity credential must also declare the "grantor"
    role (CredentialSubject.roles) before issue_operational_credential()
    will proceed. This is deliberately NOT a separate RoleCredential
    with its own status list (the alternative considered and rejected —
    see the W185 alignment response deck): a coarse role flag on the
    existing identity credential closes the same gap at a fraction of
    the schema/status-list overhead.

Two consequences worth being explicit about:
    - Revoking the Grantor's NetworkAuthorityCredential does NOT
      automatically revoke OperationalCredentials it already issued —
      those are tracked on their own status list. verifier.py's
      verify_delegation_chain() checks both independently, so a revoked
      Grantor identity does make every downstream OperationalCredential
      fail verification going forward, without needing to enumerate and
      revoke each one individually.
    - Each Grantor gets its own status list, namespaced by the Grantor's
      own agent_id (not the Governing Authority's) — see the
      authority_id passed to StatusListManager in the constructor.
"""

from __future__ import annotations

import datetime as dt
import uuid

import base58
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stls_core.auth import AuditEntry, AuthResult, default_audit_hook
from stls_core.auth.stls_core_auth_manager import AuditHook

from .stls_id_models import (
    CredentialProof,
    CredentialStatus,
    NetworkAuthorityCredential,
    OperationalCredential,
    OperationalCredentialSubject,
)
from .stls_id_status import StatusListManager

DEFAULT_OPERATIONAL_VALIDITY = dt.timedelta(days=30)
"""Deliberately shorter than DEFAULT_VALIDITY for identity credentials
(90 days, issuer.py) — an operational grant for a specific task should
not outlive the task by months. Callers can override per-call."""


class DelegationManager:
    """
    One instance per Grantor agent (not per Governing Authority — compare
    with IdentityManager, which is authority-scoped). Constructed with
    the Grantor's own signing key: the same Ed25519 key that key's DID
    Document already advertises, obtained the same way as any other
    agent's — via bootstrap_signing_key() / the identity keystore.
    """

    def __init__(
        self,
        *,
        grantor_did: str,
        grantor_signing_key: Ed25519PrivateKey,
        grantor_verification_method_id: str,
        status_list_manager: StatusListManager,
        status_list_publish_url: str,
        audit_hook: AuditHook | None = None,
    ):
        self._grantor_did = grantor_did
        self._signing_key = grantor_signing_key
        self._vm_id = grantor_verification_method_id
        self._status = status_list_manager
        self._status_list_url = status_list_publish_url
        self._audit_hook: AuditHook = audit_hook or default_audit_hook()

    async def issue_operational_credential(
        self,
        *,
        grantor_identity_credential: NetworkAuthorityCredential,
        requestor_did: str,
        requestor_client_id: str,
        scope: dict,
        valid_for: dt.timedelta = DEFAULT_OPERATIONAL_VALIDITY,
    ) -> OperationalCredential:
        """
        Args:
            grantor_identity_credential: the Grantor's OWN, currently-
                valid NetworkAuthorityCredential. The caller is expected
                to have already run Sentinel.verify() on it before
                calling this — DelegationManager checks the validity
                *window* here (cheap, no I/O) as a sanity guard, but
                does NOT re-check revocation status (that needs the
                Governing Authority's status list, which this Grantor-
                scoped manager has no access to by design).
            requestor_did: the agent receiving the delegated permission.
            scope: domain-specific grant, e.g. for eLOA cross-connect:
                {"action": "cross-connect-activate", "facility_code": "MI1",
                 "port_id": "42"}. Opaque to this module — see
                OperationalCredentialSubject's docstring.

        Raises:
            TypeError: if grantor_identity_credential is not actually a
                NetworkAuthorityCredential — this is the runtime half of
                the depth-1 enforcement described in the module
                docstring. Without this check, nothing would stop a
                caller from passing an OperationalCredential whose
                subject happens to match, defeating the structural
                argument by duck typing.
            ValueError: if grantor_identity_credential does not belong
                to this DelegationManager's own Grantor DID, or is
                outside its validity window — both are structural
                requirements for depth-1, not optional checks.
        """
        if not isinstance(grantor_identity_credential, NetworkAuthorityCredential):
            raise TypeError(
                "grantor_identity_credential must be a NetworkAuthorityCredential "
                f"(got {type(grantor_identity_credential).__name__}) — an "
                "OperationalCredential cannot itself justify issuing another "
                "OperationalCredential, that is precisely the depth-1 limit"
            )

        if grantor_identity_credential.credential_subject.id != self._grantor_did:
            raise ValueError(
                "grantor_identity_credential does not belong to this Grantor "
                f"(credential subject={grantor_identity_credential.credential_subject.id!r}, "
                f"expected={self._grantor_did!r})"
            )

        if "grantor" not in grantor_identity_credential.credential_subject.roles:
            raise ValueError(
                "grantor_identity_credential does not declare the 'grantor' role "
                f"(roles={grantor_identity_credential.credential_subject.roles!r}) — "
                "holding a valid identity is not, by itself, authorization to "
                "delegate. This is the W185 alignment fix: knowing who signed is "
                "not the same as being authorized to issue THIS kind of grant."
            )

        now = dt.datetime.now(dt.timezone.utc)
        if now < grantor_identity_credential.valid_from or (
            grantor_identity_credential.valid_until is not None
            and now > grantor_identity_credential.valid_until
        ):
            raise ValueError(
                "grantor_identity_credential is outside its validity window — "
                "a Grantor cannot delegate on an expired identity credential"
            )

        status_index = await self._status.allocate_index()

        credential = OperationalCredential(
            id=f"urn:uuid:{uuid.uuid4()}",
            issuer=self._grantor_did,
            valid_from=now,
            valid_until=now + valid_for,
            credential_subject=OperationalCredentialSubject(
                id=requestor_did,
                client_id=requestor_client_id,
                scope=scope,
            ),
            credential_status=CredentialStatus(
                id=f"{self._status_list_url}#{status_index}",
                status_list_index=status_index,
                status_list_credential=self._status_list_url,
            ),
        )

        signed = self._sign(credential)

        await self._audit_hook(AuditEntry(
            timestamp=now,
            key_id=credential.id,
            client_id=requestor_client_id,
            endpoint="stls-identity:delegate",
            result=AuthResult.OK,
        ))
        return signed

    async def revoke(self, credential: OperationalCredential) -> None:
        await self._status.revoke(credential.credential_status.status_list_index)

        await self._audit_hook(AuditEntry(
            timestamp=dt.datetime.now(dt.timezone.utc),
            key_id=credential.id,
            client_id=credential.credential_subject.client_id,
            endpoint="stls-identity:delegate-revoke",
            result=AuthResult.REVOKED,
        ))

    # -- signing -----------------------------------------------------
    # Same pattern as IdentityManager._sign() in issuer.py — kept as a
    # deliberate, small duplication rather than a premature shared
    # abstraction; extract to a common helper if a third signer appears.

    def _sign(self, credential: OperationalCredential) -> OperationalCredential:
        payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)
        canonical = rfc8785.dumps(payload)
        signature = self._signing_key.sign(canonical)

        credential.proof = CredentialProof(
            created=dt.datetime.now(dt.timezone.utc),
            verification_method=self._vm_id,
            proof_value="z" + base58.b58encode(signature).decode("ascii"),
        )
        return credential