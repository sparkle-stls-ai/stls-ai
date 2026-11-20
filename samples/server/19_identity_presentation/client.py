"""
client.py — 19_identity_presentation
---------------------------------------
Requestor: loads the identity bootstrap_identity.py wrote
(requestor_identity.json), builds an IdentityPresenter, and sends ONE
messages/send call to server.py's /secure-a2a endpoint through
STLSHttpxTransport with identity_challenge_handler=presenter.on_challenge
wired in — the full gap #1 flow: PQC handshake → first call gets
ERR_IDENTITY_CHALLENGE_REQUIRED + a fresh challenge → the transport
calls the handler, which signs a VerifiablePresentation over that
challenge and returns the X-STLS-Identity-Presentation header → the
SAME request retries automatically, exactly once, and succeeds.

Run (see README.md for the three scenarios to try):

    python client.py                  # normal: identity attached
    python client.py --no-identity    # no identity_challenge_handler at all
    python client.py --tamper         # corrupts the credential before signing
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stls_a2a.stls_a2a_secure_remote import STLSHttpxTransport, SecureSessionManager
from stls_identity import IdentityPresenter, NetworkAuthorityCredential

HERE = Path(__file__).parent
BASE_URL = "http://localhost:8019/agents/whoami/secure-a2a"


def _load_presenter(*, tamper: bool) -> IdentityPresenter:
    identity_file = HERE / "requestor_identity.json"
    if not identity_file.exists():
        raise SystemExit(f"{identity_file} not found — run `python bootstrap_identity.py` first.")
    data = json.loads(identity_file.read_text())

    holder_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["private_key_hex"]))
    credential = NetworkAuthorityCredential.model_validate(data["credential"])

    if tamper:
        # Mutates the credential AFTER it was signed — its signature no
        # longer matches, so the server must reject it. Demonstrates the
        # "rejected" outcome without needing to wire up real revocation.
        print("⚠️  --tamper: corrupting credential_subject.id after signing (expect rejection)")
        credential.credential_subject.id = credential.credential_subject.id + "-tampered"

    return IdentityPresenter(
        holder_did=data["holder_did"],
        holder_signing_key=holder_key,
        holder_verification_method_id=data["holder_verification_method_id"],
        credentials=[credential],
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-identity", action="store_true", help="never attach a presentation, even if the server asks")
    parser.add_argument("--tamper", action="store_true", help="corrupt the credential before signing (expect rejection)")
    args = parser.parse_args()

    identity_challenge_handler = None
    if not args.no_identity:
        presenter = _load_presenter(tamper=args.tamper)
        identity_challenge_handler = presenter.on_challenge

    session_manager = SecureSessionManager(client_id="19_identity_presentation-client")
    transport = STLSHttpxTransport(session_manager, identity_challenge_handler=identity_challenge_handler)

    payload = {
        "jsonrpc": "2.0",
        "method": "messages/send",
        "params": {"message": {"parts": [{"type": "text", "text": "hello from the Requestor"}]}},
        "id": 1,
    }

    try:
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                resp = await client.post(BASE_URL, json=payload)
            except httpx.HTTPStatusError as exc:
                print(f"❌ request rejected: HTTP {exc.response.status_code} — {exc.response.text}")
                return

        body = resp.json()
        if "error" in body:
            print(f"❌ request rejected: {body['error']}")
        else:
            print(f"✅ request accepted: {body.get('result')}")
    finally:
        await session_manager.aclose()


if __name__ == "__main__":
    asyncio.run(main())
