"""
run_delegation_fixture.py
-----------------------------
Pairs with grantor_agent/dump_delegation_fixture.py. Reads the
Operational Credential it issued (no chat relay) and exercises the
full identity_manager-side pipeline -- also with no chat relay:
cache_operational_credential, get_cached_operational_credential, and
validate_operational_credential against the Grantor's own cached
Identity Credential.

Run from this directory (same env as server.py):

    python run_delegation_fixture.py <grantor_agent_id>
"""

from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from tools_identity import (
    cache_operational_credential,
    get_cached_identity_credential,
    get_cached_operational_credential,
    validate_operational_credential,
)

FIXTURE_PATH = "_delegation_fixture.json"


async def main(grantor_agent_id: str) -> None:
    with open(FIXTURE_PATH) as f:
        fixture = json.load(f)
    requestor_agent_id = fixture["requestor_agent_id"]
    operational_credential = fixture["operational_credential"]
    print(f"Fixture proof present (as issued): {operational_credential.get('proof') is not None}")

    cache_result = await cache_operational_credential(requestor_agent_id, operational_credential)
    print("\ncache_operational_credential result:")
    print(json.dumps(cache_result, indent=2))
    if "error" in cache_result:
        raise SystemExit("cache_operational_credential failed")

    get_result = await get_cached_operational_credential(requestor_agent_id, cache_result["credential_id"])
    if not get_result.get("found"):
        raise SystemExit(f"get_cached_operational_credential: not found ({get_result})")
    cached_credential = get_result["credential"]
    print(f"\nproof present after cache roundtrip (no chat relay either side): {cached_credential.get('proof') is not None}")

    id_result = await get_cached_identity_credential(grantor_agent_id)
    if not id_result.get("found"):
        raise SystemExit(f"get_cached_identity_credential({grantor_agent_id!r}): not found ({id_result})")

    validation = await validate_operational_credential(cached_credential, id_result["credential"])
    print("\n--- validate_operational_credential result (no chat relay anywhere in this run) ---")
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: python {sys.argv[0]} <grantor_agent_id>")
    asyncio.run(main(sys.argv[1]))
