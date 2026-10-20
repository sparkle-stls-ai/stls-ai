"""
stls_identity.verifier
=======================

Sentinel — verifies a presented NetworkAuthorityCredential before an
agent-to-agent call is authorized.

Meant to be called as an *additional* check inside the existing
create_secure_a2a_endpoint() flow, alongside (not instead of) the current
ApiKeyManager.validate() — same fail-closed posture as HMAC-then-decrypt
in the transport layer: verify first, only proceed if every check passes.
"""

from __future__ import annotations

import datetime as dt

import base58
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .stls_id_models import (
    NetworkAuthorityCredential,
    OperationalCredential,
    VerifiableCredential,
    VerifiablePresentation,
)
from .stls_id_status import MRI_SECONDS
from .stls_id_trust_registry_client import TrustAnchorRegistryClient


class CredentialVerificationError(Exception):
    """Raised for any failure — expired, revoked, bad signature, stale
    status list. Callers should treat every subtype the same way: reject
    the request. The message is for logs/audit, not for branching logic."""


class Sentinel:
    def __init__(
        self,
        *,
        trusted_issuer_keys: dict[str, Ed25519PublicKey],
        trust_registry: TrustAnchorRegistryClient | None = None,
    ):
        """
        trusted_issuer_keys maps verification_method id -> public key —
        the fast path, checked first, no I/O. Populate it from
        TrustAnchorRegistryClient.as_trusted_keys() for the CURRENT set
        of trusted Authority keys (see trust_registry_client.py).

        trust_registry: optional fallback for verify_signature(). When a
        verification_method isn't in trusted_issuer_keys, Sentinel asks
        the registry "was this key valid for its Authority AT THE TIME
        this credential was signed?" instead of failing immediately —
        this is what makes a credential signed before an Authority
        rotated its key still verify correctly, without keeping every
        historical key loaded in the hot-path cache.

        Deliberately NOT applied to verify_presentation_proof()'s holder
        binding: the registry only tracks Governing Authority keys, never
        individual agent keys — a Requestor's own key rotation is a
        different, not-yet-built feature (agent DID Documents don't have
        key history the way Trust Anchor entries now do).
        """
        self._trusted_keys = trusted_issuer_keys
        self._trust_registry = trust_registry

    async def verify_signature(self, credential: VerifiableCredential) -> None:
        proof = credential.proof
        if proof is None:
            raise CredentialVerificationError("credential is unsigned")

        signer = self._trusted_keys.get(proof.verification_method)
        if signer is None and self._trust_registry is not None:
            # Fallback path: not a currently-trusted key — but it might
            # be one the issuing Authority has since rotated away from,
            # which is not the same as "untrusted". Resolve it against
            # the credential's OWN signing time, not "now".
            signer = await self._trust_registry.resolve_historical(
                proof.verification_method, proof.created,
            )
        if signer is None:
            raise CredentialVerificationError(
                f"unknown or untrusted verification method: {proof.verification_method}"
            )

        unsigned = credential.model_copy(update={"proof": None})
        payload = unsigned.model_dump(mode="json", by_alias=True, exclude_none=True)
        # Must match issuer.py's _sign() byte-for-byte — same RFC 8785
        # canonicalization, not a local approximation. Any divergence
        # here (even a "harmless" whitespace difference) makes every
        # signature fail to verify.
        canonical = rfc8785.dumps(payload)

        try:
            signature = base58.b58decode(proof.proof_value[1:])  # strip 'z' multibase prefix
            signer.verify(signature, canonical)
        except InvalidSignature as exc:
            raise CredentialVerificationError("signature verification failed") from exc

    def verify_validity_window(self, credential: VerifiableCredential) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        if now < credential.valid_from:
            raise CredentialVerificationError("credential not yet valid")
        if credential.valid_until is not None and now > credential.valid_until:
            raise CredentialVerificationError("credential expired")

    async def verify_not_revoked(
        self,
        credential: VerifiableCredential,
        *,
        status_bitstring_fetch_time: dt.datetime,
        is_revoked: bool,
    ) -> None:
        """Caller supplies the already-fetched status bit + its fetch
        timestamp, so the Sentinel can enforce the MRI without owning the
        HTTP fetch/cache itself (kept out of this module on purpose —
        fetching is a BackgroundRefresher-style concern)."""
        age = dt.datetime.now(dt.timezone.utc) - status_bitstring_fetch_time
        if age.total_seconds() > MRI_SECONDS:
            raise CredentialVerificationError(
                f"status list stale ({age.total_seconds():.0f}s > {MRI_SECONDS}s MRI) — fail closed"
            )
        if is_revoked:
            raise CredentialVerificationError("credential revoked")

    async def verify(
        self,
        credential: VerifiableCredential,
        *,
        status_bitstring_fetch_time: dt.datetime,
        is_revoked: bool,
    ) -> None:
        """Full check, in fail-fast order: cheapest/least-sensitive first,
        signature before trusting any other field, exactly like verifying
        HMAC before decrypting in the existing transport pipeline."""
        self.verify_validity_window(credential)
        await self.verify_signature(credential)
        await self.verify_not_revoked(
            credential,
            status_bitstring_fetch_time=status_bitstring_fetch_time,
            is_revoked=is_revoked,
        )

    async def verify_delegation_chain(
        self,
        operational_credential: OperationalCredential,
        *,
        grantor_identity_credential: NetworkAuthorityCredential,
        grantor_identity_fetch_time: dt.datetime,
        grantor_identity_is_revoked: bool,
        operational_fetch_time: dt.datetime,
        operational_is_revoked: bool,
    ) -> None:
        """
        Full Phase 3 check: an OperationalCredential is only as good as
        the Grantor's own standing at verification time — not just at
        the moment it was issued. Two independent revocation checks,
        because they live on two different status lists (see
        delegation.py's module docstring for why revoking a Grantor's
        identity doesn't retroactively revoke everything already issued
        — this method is what makes that revocation take effect anyway,
        by refusing every downstream OperationalCredential the moment
        the Grantor's identity credential itself fails verification).

        1. The Grantor's NetworkAuthorityCredential must itself verify
           (signature, validity window, not revoked) — same as any
           identity credential.
        2. The OperationalCredential's `issuer` must be exactly the DID
           the Grantor's identity credential asserts — the credential
           claiming to grant something must come from the same agent
           that holds a legitimate identity, not merely from someone
           whose signature happens to verify.
        3. The OperationalCredential must itself verify (signature,
           validity window, not revoked) — on its own, separate status
           list.
        """
        # 1. Grantor's identity, independently.
        await self.verify(
            grantor_identity_credential,
            status_bitstring_fetch_time=grantor_identity_fetch_time,
            is_revoked=grantor_identity_is_revoked,
        )

        # 2. The OperationalCredential must actually come from that DID.
        if operational_credential.issuer != grantor_identity_credential.credential_subject.id:
            raise CredentialVerificationError(
                "operational credential issuer does not match the "
                "Grantor's own identity credential subject — possible "
                "identity substitution"
            )

        # 3. The OperationalCredential itself.
        await self.verify(
            operational_credential,
            status_bitstring_fetch_time=operational_fetch_time,
            is_revoked=operational_is_revoked,
        )

    def verify_presentation_proof(
        self,
        presentation: VerifiablePresentation,
        *,
        expected_challenge: str,
    ) -> None:
        """
        Verifies the PRESENTATION's own proof — holder binding, not
        credential validity. This answers "does whoever sent this
        actually control the holder DID?", a question no bare VC answers
        on its own (see VerifiablePresentation's docstring). Credential
        validity (signature/window/revocation) is verified separately,
        via verify() or verify_delegation_chain() on the unpacked
        credentials — this method does not touch those at all.

        The holder's key is resolved through the same trusted_issuer_keys
        mapping used for credential issuers; nothing here treats a
        holder's key as structurally different from an issuer's — both
        are "a DID's key, resolved via its bridge or the registry",
        the same way stls-identity resolves any other agent's key.

        Raises:
            CredentialVerificationError: unsigned, wrong proof purpose,
                challenge mismatch (possible replay), unknown signer, or
                bad signature.
        """
        proof = presentation.proof
        if proof is None:
            raise CredentialVerificationError("presentation is unsigned")

        if proof.proof_purpose != "authentication":
            raise CredentialVerificationError(
                f"presentation proof has wrong purpose: {proof.proof_purpose!r} "
                "(expected 'authentication') — possible credential/presentation confusion"
            )

        if proof.challenge != expected_challenge:
            raise CredentialVerificationError(
                "presentation challenge does not match the one issued for this "
                "request — possible replay of a captured presentation"
            )

        signer = self._trusted_keys.get(proof.verification_method)
        if signer is None:
            raise CredentialVerificationError(
                f"unknown or untrusted holder verification method: {proof.verification_method}"
            )

        unsigned = presentation.model_copy(update={"proof": None})
        payload = unsigned.model_dump(mode="json", by_alias=True, exclude_none=True)
        canonical = rfc8785.dumps(payload)

        try:
            signature = base58.b58decode(proof.proof_value[1:])
            signer.verify(signature, canonical)
        except InvalidSignature as exc:
            raise CredentialVerificationError(
                "presentation signature verification failed"
            ) from exc

    @staticmethod
    def unpack_presentation(
        presentation: VerifiablePresentation,
    ) -> tuple[OperationalCredential, NetworkAuthorityCredential] | tuple[NetworkAuthorityCredential]:
        """
        Splits a verified presentation's credentials into the shape
        verify() / verify_delegation_chain() expect. Call this only
        AFTER verify_presentation_proof() has passed — this method does
        no cryptographic verification of its own, it only interprets
        structure.

        Returns:
            A single NetworkAuthorityCredential (plain identity call), or
            (OperationalCredential, NetworkAuthorityCredential) for a
            delegated call — in that order, regardless of how the
            Requestor ordered them in verifiable_credential.

        Raises:
            CredentialVerificationError: the credential list doesn't
                match either expected shape (e.g. two identity
                credentials, or three-plus credentials — never a valid
                presentation under this system's model).
        """
        creds = presentation.verifiable_credential

        if len(creds) == 1 and isinstance(creds[0], NetworkAuthorityCredential):
            return (creds[0],)

        if len(creds) == 2:
            operational = next((c for c in creds if isinstance(c, OperationalCredential)), None)
            identity = next((c for c in creds if isinstance(c, NetworkAuthorityCredential)), None)
            if operational is not None and identity is not None:
                return (operational, identity)

        raise CredentialVerificationError(
            f"presentation does not match a known shape: {len(creds)} credential(s) "
            f"of types {[type(c).__name__ for c in creds]} — expected either one "
            "NetworkAuthorityCredential, or one OperationalCredential plus its "
            "Grantor's NetworkAuthorityCredential"
        )