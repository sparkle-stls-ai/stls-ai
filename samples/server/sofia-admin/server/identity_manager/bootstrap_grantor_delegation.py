"""
bootstrap_grantor_delegation.py
---------------------------------
One-off setup for issue #48: produces a real Operational Credential to
validate via sofia_identity_agent (validate_operational_credential),
since identity_manager/tools_identity.py deliberately never issues one
itself (see its own docstring — issuing a delegation is the Grantor's
own act, not an identity-provisioning tool; see issue #51 for the
design question of whether that should ever change).

Run manually, once, from this directory (same env as server.py — real
Postgres/Redis, per .env):

    python bootstrap_grantor_delegation.py

Builds a DelegationManager directly, in this same process — same
"chicken-and-egg" reasoning as bootstrap_coordinator_identity.py: the
Grantor's signing key already lives in the shared identity keystore
(IdentityKeystoreFactory), the same one tools_identity.py's own
_deps() reads from, so this script can resolve it without going
through any A2A round trip.

Idempotent for the bootstrap steps (same key/DID reused on a re-run);
issues a FRESH Operational Credential every time it's run, each with
its own status-list index — re-running is safe, just produces another
valid delegation alongside any earlier one.
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

# Must run BEFORE importing tools_identity — see bootstrap_coordinator_identity.py's
# own comment: tools_identity.py never calls load_dotenv() itself.
load_dotenv()

from stls_identity import DelegationManager, NetworkAuthorityCredential, current_verification_method

from tools_identity import (
    _STATUS_LIST_PUBLISH_URL,
    _deps,
    bootstrap_agent_identity,
    issue_identity_credential_for_agent,
)

GRANTOR_AGENT_ID = "grantor-demo"
REQUESTOR_AGENT_ID = "requestor-demo"


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
        grantor_verification_method_id=current_verification_method(grantor_document).id,
        status_list_manager=d.status_list_manager,
        status_list_publish_url=_STATUS_LIST_PUBLISH_URL,
    )

    grantor_identity_credential = NetworkAuthorityCredential.model_validate(grantor_credential_payload)
    operational_credential = await delegation_manager.issue_operational_credential(
        grantor_identity_credential=grantor_identity_credential,
        requestor_did=requestor_boot["did"],
        requestor_client_id=REQUESTOR_AGENT_ID,
        scope={"action": "demo-scope", "note": "issue #48 — manual delegation validation test"},
    )
    print(f"Operational Credential issued: {operational_credential.id}")

    print("\n--- Paste these into sofia-client-secure for validate_operational_credential ---\n")
    print("operational_credential:")
    print(operational_credential.model_dump_json(by_alias=True, exclude_none=True, indent=2))
    print("\ngrantor_identity_credential:")
    print(grantor_credential_payload)


if __name__ == "__main__":
    asyncio.run(main())
