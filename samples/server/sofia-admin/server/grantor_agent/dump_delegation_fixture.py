"""
dump_delegation_fixture.py
------------------------------
Diagnostic for issue #54's live-testing session: calls
delegate_to_agent() directly, in-process, bypassing the chat agent
entirely, and writes the result to a file. Pairs with
identity_manager/run_delegation_fixture.py, which reads it and calls
cache_operational_credential + get_cached_operational_credential +
validate_operational_credential, also bypassing chat -- isolates
whether a missing `proof` on a chat-cached credential is a real bug in
delegate_to_agent/cache_operational_credential, or a chat-relay
transcription artifact (the #48 pattern), by removing chat from BOTH
ends of the pipeline.

Run from this directory (same env as server.py):

    python dump_delegation_fixture.py <requestor_agent_id>
"""

from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from tools_delegation import delegate_to_agent

OUTPUT_PATH = "../identity_manager/_delegation_fixture.json"


async def main(requestor_agent_id: str) -> None:
    result = await delegate_to_agent(
        requestor_agent_id=requestor_agent_id,
        scope_json=json.dumps({"action": "cross-connect-activate", "facility_code": "MI1"}),
    )
    if "error" in result:
        raise SystemExit(f"delegate_to_agent failed: {result['error']}")

    print("Operational Credential issued (no chat relay):")
    print(json.dumps(result, indent=2))
    print(f"\nproof present: {result.get('proof') is not None}")

    with open(OUTPUT_PATH, "w") as f:
        json.dump({"requestor_agent_id": requestor_agent_id, "operational_credential": result}, f, indent=2)
    print(f"\nWrote fixture to {OUTPUT_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: python {sys.argv[0]} <requestor_agent_id>")
    asyncio.run(main(sys.argv[1]))
