"""
revoke_grantor_identity.py
-----------------------------
Negative-case diagnostic for issue #48: revokes grantor-demo-opb's
Identity Credential directly on operator-b (no chat UI wired to this
platform), so Sparkle's validate_external_identity_credential can be
re-tried afterward and confirmed to now report invalid/revoked —
same fail-closed check already done for the internal-grantor case.

Reads the credential to revoke from the same fixture file
dump_external_delegation_fixture.py wrote (grantor_identity_credential
field) — run that script first if the fixture doesn't exist yet or is
for a different agent.

Run from this directory (same env as server.py):

    python revoke_grantor_identity.py
"""

from __future__ import annotations

import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from tools_identity import revoke_agent_identity_credential

FIXTURE_PATH = "../../_external_delegation_fixture.json"


async def main() -> None:
    with open(FIXTURE_PATH) as f:
        fixture = json.load(f)

    result = await revoke_agent_identity_credential(fixture["grantor_identity_credential"])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
