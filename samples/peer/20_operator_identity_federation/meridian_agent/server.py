"""
meridian_agent/server.py — 20_operator_identity_federation
------------------------------------------------------------
Meridian: the Verifier. A trivial "reroute_traffic" A2A agent, PQC-
encrypted, gated by stls_identity's RequestPresentationVerifier —
same wiring as samples/server/19_identity_presentation/server.py, with
one addition: the presentation Meridian expects here is always the
delegation pair (OperationalCredential + the Grantor's own
NetworkAuthorityCredential), never a plain single-credential identity
— Sentinel.unpack_presentation() tells the two shapes apart by what's
actually in the list, nothing here chooses between them up front.

Issue #74: _reroute_traffic_handler below now enforces the delegated
scope against the call's own `segment`, via stls_a2a's
verified_identity() — previously the only check at all was identity
("is there a valid grant"), never authorization ("does THIS call match
it"), so a Helios credential scoped to one segment could reroute a
different one and nothing noticed.

status_resolver below reads straight from Redis via StatusListManager,
instead of stls_identity.fetch_remote_credential_status (which would
fetch a PUBLISHED StatusListCredential document over HTTP). Standing
up a status-list-publishing HTTP endpoint is real infrastructure this
demo doesn't otherwise need; RequestPresentationVerifier's own
StatusResolver type is any `credential -> (fetch_time, is_revoked)`
callable (see its docstring: "a trivial in-memory one" is explicitly
sanctioned for tests) — reading the authoritative bit directly is
still a REAL check against REAL Redis state, just skipping the
publish/fetch round trip.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("KEY_PROVIDER_NAME", "pqc")

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import redis.asyncio as redis
import uvicorn
from fastapi import FastAPI
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from stls_a2a import (
    AgentResponse,
    KeySessionProvider,
    KeySessionProviderFactory,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
    verified_identity,
)
from stls_identity import RequestPresentationVerifier, Sentinel, StatusListManager, VerifiableCredential
from stls_mcp import perform_pqc_client_handshake, stls_httpx_factory

HERE = Path(__file__).parent
SAMPLE_ROOT = HERE.parent
PORT = int(os.getenv("PORT", "8710"))
# Base URL only, no path — the handshake and the tool call hit two
# different paths on the same host (/stls/pqc/handshake, /mcp-stls).
MERIDIAN_MCP_URL = os.getenv("MERIDIAN_MCP_URL", "http://localhost:8711")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

_authority = json.loads((SAMPLE_ROOT / "governing_authority_public.json").read_text())
_DID_TO_AUTHORITY_ID: dict[str, str] = _authority["did_to_authority_id"]

_redis_client = redis.from_url(REDIS_URL)
_status_managers: dict[str, StatusListManager] = {
    authority_id: StatusListManager(_redis_client, authority_id=authority_id)
    for authority_id in set(_DID_TO_AUTHORITY_ID.values())
}


async def _redis_status_resolver(credential: VerifiableCredential) -> tuple[dt.datetime, bool]:
    authority_id = _DID_TO_AUTHORITY_ID.get(credential.issuer)
    if authority_id is None:
        # Unknown issuer: fail closed, same posture as a status list that
        # can't be fetched at all.
        return dt.datetime.now(dt.timezone.utc), True
    is_revoked = await _status_managers[authority_id].is_revoked(
        credential.credential_status.status_list_index
    )
    return dt.datetime.now(dt.timezone.utc), is_revoked


def _build_identity_verifier() -> RequestPresentationVerifier:
    trusted_keys = {
        vm_id: Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        for vm_id, pub_hex in _authority["trusted_keys"].items()
    }
    sentinel = Sentinel(trusted_issuer_keys=trusted_keys)
    return RequestPresentationVerifier(sentinel, status_resolver=_redis_status_resolver)


async def _call_reroute_tool(segment: str, reason: str) -> dict:
    # Same-company, same-machine call — the plain /mcp endpoint would be
    # just as legitimate here (trust was already established one layer
    # up, at the A2A boundary). Uses the STLS-protected path anyway, to
    # exercise pqc on the MCP leg too, not because this call needs it.
    session_id, transport = await perform_pqc_client_handshake(
        MERIDIAN_MCP_URL, client_id="meridian-agent",
    )
    factory = stls_httpx_factory(session_id, transport)
    async with streamablehttp_client(
        url=f"{MERIDIAN_MCP_URL}/mcp-stls", httpx_client_factory=factory,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "check_and_apply_reroute", {"segment": segment, "reason": reason},
            )
            text = next(
                (c.text for c in result.content if getattr(c, "type", None) == "text"), "{}"
            )
            return json.loads(text)


async def _reroute_traffic_handler(request):
    """Plain function agent (framework='custom') — the one real skill
    this sample is about. By the time this runs, stls_a2a's identity
    gate has already verified the delegation pair AND (issue #74)
    carried the verified, unpacked scope into request.context —
    nothing here re-derives or trusts the caller's own claim about
    what it's allowed to do.

    Before #74 this handler called the tool with whatever `segment`
    the request text claimed, unchecked: identity proved WHO was
    calling and THAT a grant existed, never whether THIS call's own
    parameters matched it. A Helios credential scoped to
    {"segment": "MER-SEG-7"} could ask to reroute a different segment
    in the very same call, and nothing rejected it — exactly the gap
    the issue was filed over. verified_identity()'s `scope` is still
    opaque to stls_a2a itself (see stls_id_delegation.py's module
    docstring) — interpreting it is this handler's job, as the
    resource owner, not the identity layer's."""
    message = (request.params or {}).get("message", {})
    parts = message.get("parts", [])
    text = next((p.get("text") for p in parts if isinstance(p, dict) and p.get("text")), "{}")
    try:
        payload = json.loads(text)
        segment, reason = payload["segment"], payload["reason"]
    except (json.JSONDecodeError, KeyError):
        segment, reason = "MER-SEG-7", text

    identity = verified_identity(request) or {}
    scope = identity.get("scope") or {}
    if scope.get("action") != "reroute_traffic" or scope.get("segment") != segment:
        logger.warning(
            "Scope mismatch: requestor=%s delegated_scope=%s requested_segment=%s — denied",
            identity.get("requestor_did"), scope, segment,
        )
        return AgentResponse(
            error={
                "code": -32602,
                "message": (
                    f"delegated scope {scope!r} does not authorize "
                    f"reroute_traffic on segment {segment!r}"
                ),
            },
            id=request.id,
        )

    tool_result = await _call_reroute_tool(segment, reason)
    result = {"parts": [{"type": "text", "text": json.dumps(tool_result)}]}
    return AgentResponse(result=result, id=request.id)


def start_server() -> None:
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=PORT, name="pqc",
    )

    app = FastAPI(title="Meridian — operator identity federation sample")
    start_task_store_cleanup(app=app)

    path = "/agents/meridian/secure-a2a"
    create_secure_a2a_endpoint(
        app=app,
        agent=_reroute_traffic_handler,
        key_session_provider=key_session_provider,
        path=path,
        base_url=f"http://localhost:{PORT}",
        framework="custom",
        enable_plain_endpoint=False,
        identity_verifier=_build_identity_verifier(),
    )

    print("\n" + "=" * 70)
    print(f"Meridian (Verifier) — port {PORT}")
    print("=" * 70)
    print(f"Secure A2A: http://localhost:{PORT}{path}")
    print(f"MCP tool backend: {MERIDIAN_MCP_URL}\n")

    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    start_server()
