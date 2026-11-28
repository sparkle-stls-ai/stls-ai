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
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .stls_id_models import PRESENTATION_HEADER, VerifiableCredential, VerifiablePresentation
from .stls_id_verifier import CredentialVerificationError, Sentinel

__all__ = [
    "PresentationVerificationResult",
    "RequestPresentationVerifier",
    "StatusResolver",
    "request_presentation_verifier_from_env",
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
            await self._sentinel.verify_presentation_proof(presentation, expected_challenge=challenge)
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


def request_presentation_verifier_from_env(
    *,
    require_env_var: str = "REQUIRE_IDENTITY_PRESENTATION",
    governing_authority_did_env_var: str = "GOVERNING_AUTHORITY_DID",
    governing_authority_host_env_var: str = "GOVERNING_AUTHORITY_HOST",
    governing_authority_agent_id_env_var: str = "GOVERNING_AUTHORITY_AGENT_ID",
    identity_pg_dsn_env_var: str = "IDENTITY_PG_DSN",
) -> "RequestPresentationVerifier | None":
    """
    One-call wiring for stls-a2a's/stls-mcp's `identity_verifier` hook
    (issue #15 gap #1), entirely driven by environment variables — the
    verifier-side counterpart of identity_challenge_handler_from_env()
    (stls_id_presentation.py), generalizing the ~30 lines of
    hand-rolled Sentinel/RequestPresentationVerifier setup that used to
    be duplicated per sub-agent server (see
    samples/server/sofia-admin/server/identity_manager/server.py's
    `_build_identity_verifier()`, written before this existed).

    `require_env_var` ("false" by default): off means this returns
    None — no presentation ever checked, same off-by-default posture
    as every other piece of issue #15/#45.

    Trusts two things: the Governing Authority's own key (resolved via
    `bootstrap_signing_key()`, same signing key every agent's identity
    ultimately chains to), and — generically, for ANY caller, not just
    one hardcoded agent — whichever holder's current or historical key
    is published in its own DID Document under
    `governing_authority_host`, via `make_did_document_holder_key_resolver()`.
    That second part is why no caller-specific key ever needs
    pre-populating by hand: an already-bootstrapped caller (e.g. the
    sofia-admin coordinator, or sofia-client-secure) resolves correctly
    the first time, with no special-casing here.

    Uses `fetch_remote_credential_status` (a live HTTP fetch per
    verification, no caching) as the status resolver — the same
    tradeoff RequestPresentationVerifier's own docstring documents as
    the common case. A caller wanting a different one (e.g. a
    Redis-backed StatusListManager read, like
    samples/peer/20_operator_identity_federation/meridian_agent) should
    build RequestPresentationVerifier directly instead of this
    convenience wrapper.

    Raises:
        ValueError: `require_env_var` is true but `governing_authority_host_env_var`
            or `identity_pg_dsn_env_var` isn't set — a half-configured
            verifier would fail closed on every request anyway; fail
            loudly at setup time instead.
    """
    if os.environ.get(require_env_var, "false").strip().lower() != "true":
        return None

    host = os.environ.get(governing_authority_host_env_var, "").strip()
    pg_dsn = os.environ.get(identity_pg_dsn_env_var, "").strip()
    if not host or not pg_dsn:
        raise ValueError(
            f"{require_env_var}=true but {governing_authority_host_env_var!r} "
            f"and {identity_pg_dsn_env_var!r} must both be set to build an "
            "identity_verifier."
        )

    governing_authority_did = os.environ.get(
        governing_authority_did_env_var, f"did:web:{host}",
    ).strip()
    governing_authority_agent_id = os.environ.get(
        governing_authority_agent_id_env_var, "governing-authority",
    ).strip()
    authority_vm_id = f"{governing_authority_did}#key-1"

    from .stls_id_did import bootstrap_signing_key, make_did_document_holder_key_resolver
    from .stls_id_keystore import IdentityKeystoreFactory
    from .stls_id_postgres_store import PostgresDIDDocumentStore
    from .stls_id_status import fetch_remote_credential_status

    keystore = IdentityKeystoreFactory.build()
    _, authority_pub, _ = bootstrap_signing_key(governing_authority_agent_id, keystore)
    trusted_keys = {authority_vm_id: authority_pub}

    did_store = PostgresDIDDocumentStore(dsn=pg_dsn)
    holder_key_resolver = make_did_document_holder_key_resolver(
        did_store, governing_authority_host=host,
    )

    sentinel = Sentinel(trusted_issuer_keys=trusted_keys, holder_key_resolver=holder_key_resolver)
    return RequestPresentationVerifier(sentinel, status_resolver=fetch_remote_credential_status)
