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
import os
from typing import Awaitable, Callable, Union

import base58
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .stls_id_credential_cache import IdentityCredentialCacheFactory, OperationalCredentialCacheFactory
from .stls_id_did import domain_and_path_to_did
from .stls_id_keystore import IdentityKeystoreFactory
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


def load_identity_presenter(
    agent_id: str,
    *,
    governing_authority_host: str,
) -> IdentityPresenter | None:
    """
    Loads an already-bootstrapped-and-issued agent's identity as an
    IdentityPresenter — generalizes a pattern first written ad hoc for
    samples/server/sofia-admin's coordinator (issue #45) into a
    reusable primitive: any Requestor that already has a signing key in
    IdentityKeystoreFactory and a cached credential in
    IdentityCredentialCacheFactory can load itself with this one call,
    instead of hand-rolling key/credential/DID resolution per agent.

    Reads from the SAME two persistent stores regardless of which path
    bootstrapped/issued the identity (a one-off script, or a
    conversational tool call) — nothing here is a one-shot local-file
    snapshot, so whichever path ran most recently is what this picks up
    on the caller's next restart.

    Args:
        agent_id: the agent's own identifier — same one passed to
            bootstrap_agent_identity()/issue_identity_credential_for_agent()
            (or equivalent) when it was provisioned.
        governing_authority_host: the host segment of the Governing
            Authority's own did:web — used to recompute this agent's DID
            locally (domain_and_path_to_did() is a pure function, no I/O;
            did:web construction never needs a lookup).

    Returns:
        None if the agent has no signing key yet, or no cached
        credential yet (bootstrapped but never issued one) — the
        caller's own identity_challenge_handler should then stay
        unconfigured, same off-by-default posture as not setting one at
        all.
    """
    keystore = IdentityKeystoreFactory.build()
    holder_key = keystore.get(agent_id)
    if holder_key is None:
        return None

    cache = IdentityCredentialCacheFactory.build()
    credential = cache.get(agent_id)
    if credential is None:
        return None

    holder_did = domain_and_path_to_did(governing_authority_host, "agents", agent_id)
    return IdentityPresenter(
        holder_did=holder_did,
        holder_signing_key=holder_key,
        holder_verification_method_id=f"{holder_did}#key-1",
        credentials=[credential],
    )


def load_delegation_presenter(
    agent_id: str,
    operational_credential_id: str,
    grantor_identity_credential: NetworkAuthorityCredential,
    *,
    governing_authority_host: str,
) -> IdentityPresenter | None:
    """
    Loads an already-received Operational Credential (a delegation)
    as a ready-to-present IdentityPresenter — issue #54's follow-up to
    load_identity_presenter() above, for a Requestor presenting a
    delegated permission instead of its own plain identity.

    Deliberately does NOT resolve grantor_identity_credential itself —
    the Requestor must already have it (e.g. via
    get_cached_identity_credential(grantor_agent_id) on whichever
    platform issued the delegation). This mirrors
    validate_external_operational_credential's own design (issue #48):
    the Grantor's identity is always a separately-supplied piece of
    data, never auto-resolved by this package, since it isn't part of
    "the Requestor's own identity" — the one thing this function's
    sibling loaders are scoped to.

    Args:
        agent_id: the Requestor's own identifier — same one used to
            bootstrap its identity (its signing key must already be in
            IdentityKeystoreFactory's store).
        operational_credential_id: which cached delegation to present —
            an agent can hold several at once (OperationalCredentialCacheFactory
            is a collection, not a single slot), so the caller must say
            which one; there is no "the" delegation to default to.
        grantor_identity_credential: the Grantor's own NetworkAuthorityCredential
            that issued this delegation — resolved by the caller, not
            here (see above).
        governing_authority_host: the host segment of the Requestor's
            own Governing Authority's did:web.

    Returns:
        None if agent_id has no signing key yet, or no cached
        Operational Credential under operational_credential_id — the
        caller should then fail the same way as if no handler were
        configured at all, rather than present a half-built VP.
    """
    keystore = IdentityKeystoreFactory.build()
    holder_key = keystore.get(agent_id)
    if holder_key is None:
        return None

    cache = OperationalCredentialCacheFactory.build()
    operational_credential = cache.get(agent_id, operational_credential_id)
    if operational_credential is None:
        return None

    holder_did = domain_and_path_to_did(governing_authority_host, "agents", agent_id)
    return IdentityPresenter(
        holder_did=holder_did,
        holder_signing_key=holder_key,
        holder_verification_method_id=f"{holder_did}#key-1",
        credentials=[operational_credential, grantor_identity_credential],
    )


def identity_challenge_handler_from_env(
    *,
    agent_id_env_var: str = "IDENTITY_AGENT_ID",
    host_env_var: str = "GOVERNING_AUTHORITY_HOST",
) -> Callable[[str], Union[dict[str, str], Awaitable[dict[str, str]]]] | None:
    """
    One-call wiring for stls-a2a's `identity_challenge_handler`
    (STLSHttpxTransport/ContextualSecureRemoteA2aAgent), entirely driven
    by environment variables — no per-agent boilerplate needed in the
    caller.

    `agent_id_env_var` unset (the default, `IDENTITY_AGENT_ID`) means
    "this process presents no identity" — returns None, same
    off-by-default posture as every other piece of issue #15/#45:
    setting ONE env var is what turns identity presentation on.

    stls-a2a itself never calls this and never imports stls_identity —
    the caller imports stls_identity, calls this, and passes the result
    into stls-a2a's already-generic, duck-typed parameter:

        from stls_identity import identity_challenge_handler_from_env
        sofia_identity_agent = ContextualSecureRemoteA2aAgent(
            ...,
            identity_challenge_handler=identity_challenge_handler_from_env(),
        )

    Raises:
        ValueError: `agent_id_env_var` is set but `host_env_var` isn't —
            a half-configured identity is worse than none, fail loudly
            rather than silently presenting nothing.
    """
    agent_id = os.environ.get(agent_id_env_var, "").strip()
    if not agent_id:
        return None

    host = os.environ.get(host_env_var, "").strip()
    if not host:
        raise ValueError(
            f"{agent_id_env_var}={agent_id!r} is set but {host_env_var} is not — "
            "both are required to resolve this agent's own DID."
        )

    presenter = load_identity_presenter(agent_id, governing_authority_host=host)
    return presenter.on_challenge if presenter else None