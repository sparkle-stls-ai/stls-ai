"""
bootstrap_grantor_delegation.py
---------------------------------
One-off setup for issue #48's external-grantor case: produces a real
Operational Credential whose Grantor is an agent of OPERATOR B (not
Sparkle), to validate from Sparkle's side via
validate_external_operational_credential. Mirrors
identity_manager/bootstrap_grantor_delegation.py (the internal-grantor
case) exactly, pointed at this platform's own .env instead.

Run manually, once, from this directory (same env as server.py — real
Postgres/Redis, per .env):

    python bootstrap_grantor_delegation.py

Prints the three pieces of data validate_external_operational_credential
needs: the Operational Credential, the Grantor's Identity Credential,
and the Grantor's own DID Document (the Trust Anchor Registry only
tracks Governing Authority keys, never individual agent keys, so the
Grantor's DID Document must be supplied explicitly — same reasoning as
onboard_trust_anchor's "deliberately copied data, no automatic
resolution" design).
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

load_dotenv()

from stls_identity import DelegationManager, NetworkAuthorityCredential

from tools_identity import (
    _STATUS_LIST_PUBLISH_URL,
    _deps,
    bootstrap_agent_identity,
    issue_identity_credential_for_agent,
)

GRANTOR_AGENT_ID = "grantor-demo-opb"
REQUESTOR_AGENT_ID = "requestor-demo-opb"


async def main() -> None:
    d = await _deps()

    grantor_boot = await bootstrap_agent_identity(GRANTOR_AGENT_ID)
    if "error" in grantor_boot:
        raise SystemExit(f"bootstrap_agent_identity({GRANTOR_AGENT_ID!r}) failed: {grantor_boot['error']}")
    print(f"Grantor DID: {grantor_boot['did']} (created={grantor_boot['created']})")

    grantor_credential_payload = await issue_identity_credential_for_agent(
        GRANTOR_AGENT_ID, roles=["grantor"]
    )
    if "error" in grantor_credential_payload:
        raise SystemExit(
            f"issue_identity_credential_for_agent({GRANTOR_AGENT_ID!r}) failed: "
            f"{grantor_credential_payload['error']}"
        )
    print(f"Grantor Identity Credential issued and cached: {grantor_credential_payload['id']}")

    requestor_boot = await bootstrap_agent_identity(REQUESTOR_AGENT_ID)
    if "error" in requestor_boot:
        raise SystemExit(f"bootstrap_agent_identity({REQUESTOR_AGENT_ID!r}) failed: {requestor_boot['error']}")
    print(f"Requestor DID: {requestor_boot['did']} (created={requestor_boot['created']})")

    grantor_document = await d.did_store.get(GRANTOR_AGENT_ID)
    grantor_signing_key = d.keystore.get(GRANTOR_AGENT_ID)
    if grantor_document is None or grantor_signing_key is None:
        raise SystemExit("grantor's DID Document/signing key not found right after bootstrapping it")

    delegation_manager = DelegationManager(
        grantor_did=grantor_document.id,
        grantor_signing_key=grantor_signing_key,
        grantor_verification_method_id=grantor_document.verification_method[0].id,
        status_list_manager=d.status_list_manager,
        status_list_publish_url=_STATUS_LIST_PUBLISH_URL,
    )

    grantor_identity_credential = NetworkAuthorityCredential.model_validate(grantor_credential_payload)
    operational_credential = await delegation_manager.issue_operational_credential(
        grantor_identity_credential=grantor_identity_credential,
        requestor_did=requestor_boot["did"],
        requestor_client_id=REQUESTOR_AGENT_ID,
        scope={"action": "demo-scope", "note": "issue #48 — external grantor delegation validation test"},
    )
    print(f"Operational Credential issued: {operational_credential.id}")

    print("\n--- Paste these into sofia-client-secure for validate_external_operational_credential ---\n")
    print("operational_credential:")
    print(operational_credential.model_dump_json(by_alias=True, exclude_none=True, indent=2))
    print("\ngrantor_identity_credential:")
    print(grantor_credential_payload)
    print("\ngrantor_did_document:")
    print(grantor_document.model_dump_json(by_alias=True, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
