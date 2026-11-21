"""
run_external_delegation_fixture.py
-------------------------------------
Pairs with operator-b/identity_manager/dump_external_delegation_fixture.py.
Reads the fixture it wrote and calls validate_external_operational_credential
directly, in-process, with properly-deserialized dicts — no chat agent
in between to possibly reformat the credentials before they reach the
verifier. If this passes while the same data failed via
sofia-client-secure with "signature verification failed", the earlier
failure was a chat-relay artifact, not a real bug in
validate_external_operational_credential. If this ALSO fails, it's a
real bug to track down here instead.

Run from this directory (same env as server.py — real Postgres/Redis,
same Trust Anchor Registry operator-b's Authority must already be
onboarded into):

    python run_external_delegation_fixture.py
"""

from __future__ import annotations

import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from tools_identity import validate_external_operational_credential

FIXTURE_PATH = "../_external_delegation_fixture.json"


async def main() -> None:
    with open(FIXTURE_PATH) as f:
        fixture = json.load(f)

    result = await validate_external_operational_credential(
        operational_credential=fixture["operational_credential"],
        grantor_identity_credential=fixture["grantor_identity_credential"],
        grantor_did_document=fixture["grantor_did_document"],
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
