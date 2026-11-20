"""
server.py — 19_identity_presentation
---------------------------------------
Verifier agent: a trivial "whoami" A2A agent exposed over /secure-a2a,
with the new optional identity_verifier hook wired in (issue #15
gap #1 — RequestPresentationVerifier, stls_identity's bridge to
Sentinel). Reads authority_public.json (written by
bootstrap_identity.py) to build its trusted_issuer_keys.

IDENTITY_ENABLED=false disables the hook entirely
(identity_verifier=None passed to create_secure_a2a_endpoint) — this
is the off-by-default guarantee from the design: every existing
agent/sample keeps working completely unchanged unless identity is
explicitly wired in, same posture ApiKeyManager already has. Run this
sample both ways (see README.md) to see that directly rather than
just take it on faith.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import uvicorn
from fastapi import FastAPI
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from stls_a2a import (
    AgentResponse,
    KeySessionProvider,
    KeySessionProviderFactory,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
)
from stls_identity import RequestPresentationVerifier, Sentinel, VerifiableCredential

HERE = Path(__file__).parent
PORT = int(os.getenv("PORT", "8019"))
IDENTITY_ENABLED = os.getenv("IDENTITY_ENABLED", "true").strip().lower() != "false"


async def _whoami_handler(request):
    """Plain function agent (framework='custom') — just echoes the
    text back. The point of this sample is the identity GATE in front
    of it, not the agent itself."""
    message = (request.params or {}).get("message", {})
    parts = message.get("parts", [])
    text = next((p.get("text") for p in parts if isinstance(p, dict) and p.get("text")), "(no text)")
    result = {"parts": [{"type": "text", "text": f"whoami-agent received: {text!r}"}]}
    return AgentResponse(result=result, id=request.id)


async def _never_revoked(credential: VerifiableCredential) -> tuple[dt.datetime, bool]:
    """
    Trivial, explicitly-named stand-in for a real status_resolver — see
    RequestPresentationVerifier's own docstring: stls-identity has no
    remote status-list fetcher yet (only the issuer side publishes
    one). A real deployment MUST supply its own resolver that actually
    fetches and checks the issuing Authority's published
    BitstringStatusList; this sample never revokes anything, so this
    stub is honestly correct FOR THIS SAMPLE ONLY, not a pattern to
    copy as-is into production.
    """
    return dt.datetime.now(dt.timezone.utc), False


def _build_identity_verifier() -> RequestPresentationVerifier | None:
    if not IDENTITY_ENABLED:
        logger.info("IDENTITY_ENABLED=false — identity_verifier is None, hook fully disabled.")
        return None

    authority_file = HERE / "authority_public.json"
    if not authority_file.exists():
        raise SystemExit(
            f"{authority_file} not found — run `python bootstrap_identity.py` first."
        )
    authority = json.loads(authority_file.read_text())
    # Includes the Requestor's own verification key, not just the
    # Authority's — Sentinel resolves a presentation's HOLDER through
    # this same mapping (see _build_identity_verifier's module-level
    # note and RequestPresentationVerifier's docstring).
    trusted_keys = {
        vm_id: Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        for vm_id, pub_hex in authority["trusted_keys"].items()
    }
    sentinel = Sentinel(trusted_issuer_keys=trusted_keys)
    return RequestPresentationVerifier(sentinel, status_resolver=_never_revoked)


def start_server() -> None:
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=PORT,
    )

    app = FastAPI(title="Identity Presentation sample — Verifier agent")
    start_task_store_cleanup(app=app)

    path = "/agents/whoami/secure-a2a"
    create_secure_a2a_endpoint(
        app=app,
        agent=_whoami_handler,
        key_session_provider=key_session_provider,
        path=path,
        base_url=f"http://localhost:{PORT}",
        framework="custom",
        enable_plain_endpoint=False,  # identity is a /secure-a2a-only concern
        identity_verifier=_build_identity_verifier(),
    )

    print("\n" + "=" * 70)
    print(f"Identity Presentation sample (issue #15 gap #1) — port {PORT}")
    print(f"identity_verifier: {'ENABLED' if IDENTITY_ENABLED else 'disabled (default behavior)'}")
    print("=" * 70)
    print(f"Secure A2A: http://localhost:{PORT}{path}\n")

    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    start_server()
