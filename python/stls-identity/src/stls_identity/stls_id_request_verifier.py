"""
stls_identity.a2a_request_verifier
====================================

Bridge between Sentinel (this package's verification engine) and
stls-a2a's optional identity_verifier hook (issue #15 gap #1 — see
SecureA2AHTTPMiddleware._check_identity_async() in
stls_a2a_integration.py).

stls-a2a defines that hook purely as a duck type (`async def
verify_request(request, *, challenge: str) -> result` where result has
`.ok: bool` / `.error: str | None`) and never imports stls_identity —
same posture it already has toward ApiKeyManager. This module is the
one-way dependency that makes the hook concrete: it imports nothing
from stls_a2a (only reads `request.headers`, which any ASGI request
object exposes), so stls-identity stays fully usable without stls-a2a
installed.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .stls_id_models import PRESENTATION_HEADER, VerifiableCredential, VerifiablePresentation
from .stls_id_verifier import CredentialVerificationError, Sentinel

__all__ = [
    "PresentationVerificationResult",
    "RequestPresentationVerifier",
    "StatusResolver",
]


@dataclass
class PresentationVerificationResult:
    """Matches the minimal duck type stls-a2a's
    SecureA2AHTTPMiddleware._check_identity_async() expects back from
    identity_verifier.verify_request()."""

    ok: bool
    error: str | None = None


StatusResolver = Callable[[VerifiableCredential], Awaitable[tuple[dt.datetime, bool]]]
"""Caller-supplied: given a credential, returns
(status_bitstring_fetch_time, is_revoked) — exactly the two inputs
Sentinel.verify()/verify_not_revoked() already require explicitly as
arguments (stls_id_verifier.py). No default is provided here on
purpose: this package has no remote status-list fetcher yet (only the
issuer side publishes a BitstringStatusList — see status.py; nothing
resolves a REMOTE authority's published one for a verifying bridge
like this). Silently treating "no resolver" as "not revoked" would be
an insecure default a caller could easily miss; requiring an
explicitly-named one keeps the limitation visible in their own code
instead (see tests/test_request_verifier.py for a trivial in-memory
one)."""


class RequestPresentationVerifier:
    """
    Verifies the VerifiablePresentation attached to an incoming A2A
    request's PRESENTATION_HEADER, end to end: holder-binding proof,
    then the unpacked credential(s)' own signature/validity/revocation.

    One instance per Sentinel (i.e. per set of trusted issuer keys) is
    meant to be constructed once and reused across requests, same as
    Sentinel itself.
    """

    def __init__(self, sentinel: Sentinel, status_resolver: StatusResolver):
        self._sentinel = sentinel
        self._status_resolver = status_resolver

    async def verify_request(self, request: Any, *, challenge: str) -> PresentationVerificationResult:
        header = request.headers.get(PRESENTATION_HEADER, "").strip()
        if not header:
            return PresentationVerificationResult(ok=False, error="missing presentation header")

        try:
            raw = base64.b64decode(header, validate=True)
            presentation = VerifiablePresentation.model_validate(json.loads(raw))
        except (binascii.Error, ValueError, TypeError) as exc:
            return PresentationVerificationResult(ok=False, error=f"malformed presentation: {exc}")

        try:
            self._sentinel.verify_presentation_proof(presentation, expected_challenge=challenge)
            shape = self._sentinel.unpack_presentation(presentation)

            if len(shape) == 1:
                (identity,) = shape
                fetch_time, revoked = await self._status_resolver(identity)
                await self._sentinel.verify(
                    identity, status_bitstring_fetch_time=fetch_time, is_revoked=revoked,
                )
            else:
                operational, grantor_identity = shape
                grantor_fetch_time, grantor_revoked = await self._status_resolver(grantor_identity)
                operational_fetch_time, operational_revoked = await self._status_resolver(operational)
                await self._sentinel.verify_delegation_chain(
                    operational,
                    grantor_identity_credential=grantor_identity,
                    grantor_identity_fetch_time=grantor_fetch_time,
                    grantor_identity_is_revoked=grantor_revoked,
                    operational_fetch_time=operational_fetch_time,
                    operational_is_revoked=operational_revoked,
                )
        except CredentialVerificationError as exc:
            return PresentationVerificationResult(ok=False, error=str(exc))

        return PresentationVerificationResult(ok=True)
