"""
stls_identity.issuer
=====================

IdentityManager — the Grantor-side counterpart to today's ApiKeyManager.

Design mirrors ApiKeyManager deliberately:
    ApiKeyManager.validate()        <->  Sentinel.verify()          (verifier.py)
    ApiKeyManager's KeyRecord       <->  NetworkAuthorityCredential  (models.py)
    ApiKeyManager.audit_log()       <->  IdentityManager logs issuance the same way
    RedisKeyCache / stls:revoked    <->  StatusListManager           (status.py)

Both systems run in parallel through the whole migration: issuing a
NetworkAuthorityCredential here does NOT touch KeyRecord, RedisKeyCache,
or the existing /a2a auth path in any way.
"""

from __future__ import annotations

import datetime as dt
import uuid

import base58
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Real stls-core audit primitives — same AuditEntry/AuthResult schema
# ApiKeyManager already writes to, so credential issuance/revocation land
# in the same audit stream, not a parallel one. AuthResult is a closed
# enum owned by stls-core: we deliberately reuse OK/REVOKED rather than
# inventing new members stls-core doesn't know about.
from stls_core.auth import AuditEntry, AuthResult, default_audit_hook
from stls_core.auth.stls_core_auth_manager import AuditHook

from .stls_id_models import (
    CredentialProof,
    CredentialStatus,
    CredentialSubject,
    NetworkAuthorityCredential,
)
from .stls_id_status import StatusListManager

DEFAULT_VALIDITY = dt.timedelta(days=90)  # independent of API key expires_at by design


class IdentityManager:
    """One instance per Governing Authority. Holds the Ed25519 signing
    key used to sign every credential this authority issues."""

    def __init__(
        self,
        *,
        governing_authority_did: str,
        signing_key: Ed25519PrivateKey,
        signing_verification_method_id: str,
        status_list_manager: StatusListManager,
        status_list_publish_url: str,
        audit_hook: AuditHook | None = None,
    ):
        self._issuer_did = governing_authority_did
        self._signing_key = signing_key
        self._vm_id = signing_verification_method_id
        self._status = status_list_manager
        self._status_list_url = status_list_publish_url
        # Defaults to the same logging-based hook ApiKeyManager uses —
        # swap for a SIEM/Pub-Sub hook in production, same as there.
        self._audit_hook: AuditHook = audit_hook or default_audit_hook()

    async def issue_identity_credential(
        self,
        *,
        agent_did: str,
        client_id: str,  # bridges to the existing KeyRecord.client_id
        valid_for: dt.timedelta = DEFAULT_VALIDITY,
        roles: list[str] | None = None,
        principal_ref: str | None = None,
    ) -> NetworkAuthorityCredential:
        """Issue a NetworkAuthorityCredential for one agent.

        Mirrors ApiKeyManager's posture: the credential id is server-
        generated (urn:uuid), never client-controlled — same rationale as
        session_id being UUID4-server-side in the handshake today.

        Args:
            roles: coarse-grained role declaration (e.g. ["grantor"]).
                Required (must include "grantor") if this agent will
                ever call DelegationManager.issue_operational_credential()
                — see that method's role check. Empty by default: an
                agent that never delegates doesn't need any role at all.
            principal_ref: opaque reference to the human/org principal
                behind this agent (e.g. "org:sparkle:bda"). Accountability
                metadata only — not checked by this method or by Sentinel.
        """
        now = dt.datetime.now(dt.timezone.utc)
        status_index = await self._status.allocate_index()

        credential = NetworkAuthorityCredential(
            id=f"urn:uuid:{uuid.uuid4()}",
            issuer=self._issuer_did,
            valid_from=now,
            valid_until=now + valid_for,
            credential_subject=CredentialSubject(
                id=agent_did,
                client_id=client_id,
                roles=roles or [],
                principal_ref=principal_ref,
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
            key_id=credential.id,       # not a KeyRecord key_id, but same
                                          # field slot — the credential id
                                          # is this system's equivalent
            client_id=client_id,
            endpoint="stls-identity:issue",
            result=AuthResult.OK,
        ))
        return signed

    async def revoke(self, credential: NetworkAuthorityCredential) -> None:
        await self._status.revoke(credential.credential_status.status_list_index)

        await self._audit_hook(AuditEntry(
            timestamp=dt.datetime.now(dt.timezone.utc),
            key_id=credential.id,
            client_id=credential.credential_subject.client_id,
            endpoint="stls-identity:revoke",
            result=AuthResult.REVOKED,
        ))

    # -- signing -----------------------------------------------------

    def _sign(self, credential: NetworkAuthorityCredential) -> NetworkAuthorityCredential:
        """Detached Ed25519 signature over the JCS-canonicalized (RFC 8785)
        JSON of the credential, proof field excluded.

        Uses the `rfc8785` package — a real RFC 8785 implementation, not
        an approximation. A plain `json.dumps(sort_keys=True)` looks
        equivalent for simple payloads but silently diverges on number
        formatting and non-ASCII string escaping, which would make a
        credential signed by one platform fail verification on another
        during federation, even though both sides "canonicalized" it.
        JCS is the exact mechanism that must be byte-identical across
        independently-implemented bridges for that not to happen."""
        payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)
        canonical = rfc8785.dumps(payload)
        signature = self._signing_key.sign(canonical)

        credential.proof = CredentialProof(
            created=dt.datetime.now(dt.timezone.utc),
            verification_method=self._vm_id,
            proof_value="z" + base58.b58encode(signature).decode("ascii"),
        )
        return credential