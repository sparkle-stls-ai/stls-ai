"""
bootstrap_sofia_client_secure_identity.py
-------------------------------------------
One-off setup (issue #42 follow-up): gives sofia-client-secure's own
Python secure transport service (secure_transport.py) a DID +
NetworkAuthorityCredential, so it can present a VerifiablePresentation
to the coordinator once REQUIRE_IDENTITY_PRESENTATION=true there (see
coordinator/server.py's own _build_identity_verifier()).

Run manually, once, from this directory (same env as server.py — real
Postgres/Redis, per .env):

    python bootstrap_sofia_client_secure_identity.py

Same chicken-and-egg reasoning as bootstrap_coordinator_identity.py
(see that script's own docstring): bootstrap_agent_identity/
issue_identity_credential_for_agent are exposed BY identity_manager's
own endpoint, so sofia-client-secure can't call them over A2A to get
an identity it doesn't have yet — this script calls tools_identity.py
directly, in the same process, no A2A round trip.

Deliberately NOT reading the shared IDENTITY_AGENT_ID env var
bootstrap_coordinator_identity.py uses: that name is already claimed
by the coordinator's own identity in this same identity_manager's
.env (see tools_identity.py's own module docstring on IDENTITY_AGENT_ID's
dual role). A second identity needs its own, differently-named
variable so the two never collide.

Writes nothing of its own: issue_identity_credential_for_agent already
caches the credential it issues, and the private key lives in the
identity keystore bootstrap_agent_identity() already wrote to — both
in the SAME Redis sofia-client-secure's own secure_transport.py reads
from (its .env must point at that same Redis/namespace — see that
sample's own .env.example). Re-running this script is safe — idempotent
key, freshly re-issued and re-cached credential each time.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

# Must run BEFORE importing tools_identity — see
# bootstrap_coordinator_identity.py's own docstring for why.
load_dotenv()

from tools_identity import (
    bootstrap_agent_identity,
    issue_identity_credential_for_agent,
)

AGENT_ID = os.environ.get("SOFIA_CLIENT_SECURE_AGENT_ID", "sofia-client-secure")


async def main() -> None:
    boot = await bootstrap_agent_identity(AGENT_ID)
    if "error" in boot:
        raise SystemExit(f"bootstrap_agent_identity failed: {boot['error']}")
    print(f"DID: {boot['did']} (created={boot['created']})")

    credential = await issue_identity_credential_for_agent(AGENT_ID, roles=[])
    if "error" in credential:
        raise SystemExit(f"issue_identity_credential_for_agent failed: {credential['error']}")
    print(f"Issued and cached credential: {credential['id']}")
    print(
        "sofia-client-secure identity ready — restart secure_transport.py "
        "(python/secure_transport.py) to pick it up."
    )


if __name__ == "__main__":
    asyncio.run(main())
