"""
bootstrap_coordinator_identity.py
-----------------------------------
One-off setup for issue #45: gives the Sofia Admin coordinator
(agent_coordinator.py) its own DID + NetworkAuthorityCredential, so it
can present a VerifiablePresentation to identity_manager once
REQUIRE_IDENTITY_PRESENTATION=true there.

Run manually, once, from this directory (same env as server.py — real
Postgres/Redis, per .env):

    python bootstrap_coordinator_identity.py

Why this can't just be a tool call over /secure-a2a: bootstrap_agent_identity
and issue_identity_credential_for_agent are exposed BY identity_manager's
own (about-to-require-identity) endpoint — the coordinator can't call
them to get an identity it doesn't have yet. This script calls
tools_identity.py's functions directly, in the same process, no A2A
round trip, exactly like samples/server/19_identity_presentation's own
bootstrap_identity.py solves the same chicken-and-egg problem.

Writes nothing of its own: issue_identity_credential_for_agent already
caches the credential it issues (same Redis the coordinator's own
agent_coordinator.py reads from — see _load_coordinator_presenter()
there), and the private key lives in the identity keystore
bootstrap_agent_identity() already wrote to. Re-running this script is
safe — bootstrap_agent_identity() is idempotent (same key, no new one
generated on an agent_id that already exists); only the credential is
freshly re-issued and re-cached each time, exactly as if it had been
issued through a chat call to sofia_identity_agent instead — both
paths converge on the same stores, so either one keeps the
coordinator working.
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

# Must run BEFORE importing tools_identity: unlike agent_identity.py/server.py
# (which both call this themselves before importing tools_identity),
# tools_identity.py never loads .env on its own — it only reads
# os.environ via os.environ.get(...) at import/call time. Importing it
# directly, as this standalone script does, would otherwise see every
# var as unset (found the hard way: "IDENTITY_PG_DSN not configured").
load_dotenv()

from tools_identity import (
    IDENTITY_AGENT_ID,
    bootstrap_agent_identity,
    issue_identity_credential_for_agent,
)


async def main() -> None:
    boot = await bootstrap_agent_identity(IDENTITY_AGENT_ID)
    if "error" in boot:
        raise SystemExit(f"bootstrap_agent_identity failed: {boot['error']}")
    print(f"DID: {boot['did']} (created={boot['created']})")

    credential = await issue_identity_credential_for_agent(IDENTITY_AGENT_ID, roles=[])
    if "error" in credential:
        raise SystemExit(f"issue_identity_credential_for_agent failed: {credential['error']}")
    print(f"Issued and cached credential: {credential['id']}")
    print("Coordinator identity ready — restart agent_coordinator.py to pick it up.")


if __name__ == "__main__":
    asyncio.run(main())
