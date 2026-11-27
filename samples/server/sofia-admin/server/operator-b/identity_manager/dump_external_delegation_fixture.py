"""
dump_external_delegation_fixture.py
--------------------------------------
Diagnostic for issue #48's external-grantor case after hitting
"signature verification failed" via sofia-client-secure: writes the
exact dicts validate_external_operational_credential needs to a JSON
file, via proper model_dump(mode="json", ...) — never through a chat
agent's relay of printed text, which can silently reformat/reorder
fields and break RFC 8785 canonicalization. Pairs with
identity_manager/run_external_delegation_fixture.py (Sparkle side),
which reads this file and calls validate_external_operational_credential
directly, in-process — isolating whether the earlier failure was a
real bug or a chat-paste artifact.

Run from this directory (same env as server.py):

    python dump_external_delegation_fixture.py
"""

from __future__ import annotations

import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from stls_identity import DelegationManager, NetworkAuthorityCredential, current_verification_method

from tools_identity import (
    _STATUS_LIST_PUBLISH_URL,
    _deps,
    bootstrap_agent_identity,
    issue_identity_credential_for_agent,
)

GRANTOR_AGENT_ID = "grantor-demo-opb"
REQUESTOR_AGENT_ID = "requestor-demo-opb"
OUTPUT_PATH = "../../_external_delegation_fixture.json"


async def main() -> None:
    d = await _deps()

    grantor_boot = await bootstrap_agent_identity(GRANTOR_AGENT_ID)
    if "error" in grantor_boot:
        raise SystemExit(f"bootstrap_agent_identity failed: {grantor_boot['error']}")

    grantor_credential_payload = await issue_identity_credential_for_agent(
        GRANTOR_AGENT_ID, roles=["grantor"]
    )
    if "error" in grantor_credential_payload:
        raise SystemExit(f"issue_identity_credential_for_agent failed: {grantor_credential_payload['error']}")

    requestor_boot = await bootstrap_agent_identity(REQUESTOR_AGENT_ID)
    if "error" in requestor_boot:
        raise SystemExit(f"bootstrap_agent_identity failed: {requestor_boot['error']}")

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
        scope={"action": "demo-scope", "note": "issue #48 diagnostic"},
    )

    fixture = {
        "operational_credential": operational_credential.model_dump(mode="json", by_alias=True, exclude_none=True),
        "grantor_identity_credential": grantor_credential_payload,
        "grantor_did_document": grantor_document.model_dump(mode="json", by_alias=True),
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(fixture, f, indent=2)
    print(f"Wrote fixture to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
