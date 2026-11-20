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

import base64
import datetime as dt
import json

import base58
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .stls_id_models import (
    NetworkAuthorityCredential,
    OperationalCredential,
    PRESENTATION_HEADER,
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

    SECURITY: `challenge` (and the proof's other fields) MUST be part of
    what gets signed, not appended afterward — otherwise anyone holding
    a captured, validly-signed VP could overwrite `proof.challenge` with
    a newly-issued one and replay it, no private key needed, since the
    signature itself would never change. Fixed by signing over the
    proof-minus-proofValue alongside the presentation, mirrored exactly
    in Sentinel.verify_presentation_proof() (stls_id_verifier.py) — both
    sides must canonicalize the identical shape or every signature fails
    to verify.
    """
    now = dt.datetime.now(dt.timezone.utc)
    vp = VerifiablePresentation(holder=holder_did, verifiable_credential=credentials)
    vp.proof = PresentationProof(
        created=now,
        verification_method=holder_verification_method_id,
        challenge=challenge,
        proof_value="",  # placeholder — cannot sign over its own value
    )

    payload = vp.model_dump(mode="json", by_alias=True, exclude_none=True)
    del payload["proof"]["proofValue"]
    canonical = rfc8785.dumps(payload)
    signature = holder_signing_key.sign(canonical)

    vp.proof.proof_value = "z" + base58.b58encode(signature).decode("ascii")
    return vp


def attach_identity_presentation_header(vp: VerifiablePresentation) -> dict[str, str]:
    """
    Encodes a signed VerifiablePresentation for the wire (issue #15
    gap #1): base64 of its JSON serialization, under PRESENTATION_HEADER
    — the exact inverse of what RequestPresentationVerifier.verify_request()
    decodes server-side (a2a_request_verifier.py).
    """
    payload = vp.model_dump(mode="json", by_alias=True, exclude_none=True)
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {PRESENTATION_HEADER: encoded}


class IdentityPresenter:
    """
    Requestor-side convenience: holds one agent's holder identity
    (DID, signing key, credential(s) to present) and turns a
    server-issued challenge into ready-to-send headers.

    Built to be passed directly as stls-a2a's
    `identity_challenge_handler` (STLSHttpxTransport/
    ContextualSecureRemoteA2aAgent) — `presenter.on_challenge` matches
    that hook's `Callable[[str], dict[str, str]]` shape exactly, so
    stls-a2a never needs to import stls_identity itself.
    """

    def __init__(
        self,
        *,
        holder_did: str,
        holder_signing_key: Ed25519PrivateKey,
        holder_verification_method_id: str,
        credentials: list[NetworkAuthorityCredential | OperationalCredential],
    ):
        self._holder_did = holder_did
        self._holder_signing_key = holder_signing_key
        self._holder_verification_method_id = holder_verification_method_id
        self._credentials = credentials

    def on_challenge(self, challenge: str) -> dict[str, str]:
        """
        Builds a fresh VerifiablePresentation signed over `challenge`
        (a new one every call — build_verifiable_presentation() never
        reuses a signature across challenges, see its own docstring)
        and returns the header dict ready to merge into a request.
        """
        vp = build_verifiable_presentation(
            holder_did=self._holder_did,
            holder_signing_key=self._holder_signing_key,
            holder_verification_method_id=self._holder_verification_method_id,
            credentials=self._credentials,
            challenge=challenge,
        )
        return attach_identity_presentation_header(vp)