"""
stls_identity.presentation
============================

Requestor-side construction of a VerifiablePresentation — the bundle
that actually travels with an A2A request (see models.py's
VerifiablePresentation docstring, and the "Cosa Viaggia, Cosa È
Pubblicato" slide).

This closes the previously-open question: "how does a Requestor attach
both an OperationalCredential and its Grantor's NetworkAuthorityCredential
to a single call?" — it builds one VerifiablePresentation containing
both, signed once with the Requestor's own key.
"""

from __future__ import annotations

import datetime as dt

import base58
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .stls_id_models import (
    NetworkAuthorityCredential,
    OperationalCredential,
    PresentationProof,
    VerifiablePresentation,
)


def build_verifiable_presentation(
    *,
    holder_did: str,
    holder_signing_key: Ed25519PrivateKey,
    holder_verification_method_id: str,
    credentials: list[NetworkAuthorityCredential | OperationalCredential],
    challenge: str,
) -> VerifiablePresentation:
    """
    Builds and signs a VerifiablePresentation.

    Args:
        holder_did: the DID of the agent presenting this VP — the
            Requestor. Must be the DID whose key signs it (holder
            binding is what makes a captured VC unusable by anyone
            other than its rightful holder).
        credentials: for a plain identity call, a single-item list with
            the Requestor's own NetworkAuthorityCredential. For a
            delegated call, exactly two items: the OperationalCredential
            being presented, and the Grantor's own NetworkAuthorityCredential
            — both are required for the verifier's delegation-chain check.
        challenge: the nonce supplied by the verifier for THIS request
            (same role as HandshakeRequest.nonce in stls_core_handshake).
            The caller is responsible for obtaining it from whoever will
            verify this VP — this function does not fetch or generate one,
            reusing a challenge across requests would defeat its purpose.
    """
    now = dt.datetime.now(dt.timezone.utc)
    vp = VerifiablePresentation(holder=holder_did, verifiable_credential=credentials)

    payload = vp.model_dump(mode="json", by_alias=True, exclude_none=True)
    canonical = rfc8785.dumps(payload)
    signature = holder_signing_key.sign(canonical)

    vp.proof = PresentationProof(
        created=now,
        verification_method=holder_verification_method_id,
        challenge=challenge,
        proof_value="z" + base58.b58encode(signature).decode("ascii"),
    )
    return vp