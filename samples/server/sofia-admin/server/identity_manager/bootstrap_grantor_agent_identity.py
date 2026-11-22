"""
bootstrap_grantor_agent_identity.py
---------------------------------------
One-off provisioning for a real Grantor agent (issue #51) — gives it a
DID + a NetworkAuthorityCredential declaring the "grantor" role, so
grantor_agent/server.py can load it at startup via
stls_identity.load_delegation_manager() and actually issue delegations.

Unlike bootstrap_coordinator_identity.py (always the ONE coordinator),
there can be many different Grantors (one per facility/network
segment/business authority), so the agent_id is a required CLI
argument here, not a hardcoded constant.

Run manually, once per Grantor, from this directory (same env as
server.py — real Postgres/Redis, per .env):

    python bootstrap_grantor_agent_identity.py grantor-mi1

Same chicken-and-egg reasoning as bootstrap_coordinator_identity.py:
bootstrap_agent_identity/issue_identity_credential_for_agent are
exposed BY identity_manager's own endpoint — a not-yet-provisioned
Grantor can't call them over A2A to get its own identity. This script
calls tools_identity.py's functions directly, in the same process, no
round trip.

Re-running for the same agent_id is safe — bootstrap_agent_identity()
is idempotent (same key, no new one generated), and re-issuing just
refreshes the credential (new validity window), exactly as if done via
a chat call to sofia_identity_agent instead.
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from tools_identity import bootstrap_agent_identity, issue_identity_credential_for_agent


async def main(agent_id: str) -> None:
    boot = await bootstrap_agent_identity(agent_id)
    if "error" in boot:
        raise SystemExit(f"bootstrap_agent_identity failed: {boot['error']}")
    print(f"DID: {boot['did']} (created={boot['created']})")

    credential = await issue_identity_credential_for_agent(agent_id, roles=["grantor"])
    if "error" in credential:
        raise SystemExit(f"issue_identity_credential_for_agent failed: {credential['error']}")
    print(f"Issued and cached Grantor Identity Credential: {credential['id']}")
    print(f"{agent_id!r} is ready — start grantor_agent/server.py with IDENTITY_AGENT_ID={agent_id} to use it.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: python {sys.argv[0]} <grantor_agent_id>")
    asyncio.run(main(sys.argv[1]))
