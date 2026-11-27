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
    identity_verifier.verify_request() — `.ok`/`.error` is the part
    that gate has always read; `requestor_did`/`grantor_did`/`scope`
    are additive (issue #74) and exist so the caller (stls-a2a,
    stls-mcp) can carry the VERIFIED, unpacked credential past the
    gate to wherever the real effect happens, instead of discarding it
    the moment `.ok` is True.

    scope stays exactly as opaque here as it already was to Sentinel
    (see stls_id_delegation.py's module docstring) — this result
    hands it through verbatim, it never inspects or interprets it.
    """

    ok: bool
    error: str | None = None
    requestor_did: str | None = None
    """The presenting holder's own DID, set whenever ok=True — the
    Grantor's DID for a plain identity presentation, the Requestor's
    DID for a delegated one (the one actually making the request
    either way)."""
    grantor_did: str | None = None
    """Set only for a delegated (OperationalCredential + Grantor
    identity) presentation — None for a plain identity presentation,
    since there's no delegation involved."""
    scope: dict[str, Any] | None = None
    """The OperationalCredential's own `scope`, verbatim — None for a
    plain identity presentation (there's no grant to carry), a dict
    (possibly empty) for a delegated one. None is the signal "this
    caller presented its own identity, not a delegated grant";
    distinguishing that from an intentionally empty `{}` grant is why
    this isn't defaulted to `{}`."""


StatusResolver = Callable[[VerifiableCredential], Awaitable[tuple[dt.datetime, bool]]]
"""Caller-supplied: given a credential, returns
(status_bitstring_fetch_time, is_revoked) — exactly the two inputs
Sentinel.verify()/verify_not_revoked() already require explicitly as
arguments (stls_id_verifier.py). No default is provided here on
purpose, even though a real one now exists
(fetch_remote_credential_status, stls_id_status.py): silently
defaulting to it would let a caller end up doing a live HTTP fetch per
verification without ever having decided that's the right tradeoff for
their deployment (caching policy, failure mode on an unreachable
status list, etc.) — requiring an explicit resolver keeps that choice
visible in their own code. Pass fetch_remote_credential_status
directly for the common case, or see
tests/test_request_verifier.py for a trivial in-memory one."""


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
                return PresentationVerificationResult(
                    ok=True, requestor_did=identity.credential_subject.id,
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
                return PresentationVerificationResult(
                    ok=True,
                    requestor_did=operational.credential_subject.id,
                    grantor_did=grantor_identity.credential_subject.id,
                    scope=operational.credential_subject.scope,
                )
        except CredentialVerificationError as exc:
            return PresentationVerificationResult(ok=False, error=str(exc))
