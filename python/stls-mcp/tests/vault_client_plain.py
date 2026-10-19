"""
vault_client_plain.py
----------------------
Discovery client for the "Deep Sky Vault" example server
(vault_server.py). Identical to vault_client.py but uses the plain
/mcp endpoint (standard MCP, no STLS encryption) instead of /mcp-stls.

Usage:
    # terminal 1
    python tests/vault_server.py

    # terminal 2
    python tests/vault_client_plain.py

Useful for:
  - verifying that the plain /mcp endpoint is reachable and working
    independently of the STLS layer (regression test)
  - comparing behavior against vault_client.py (encrypted endpoint)
  - testing with standard MCP clients that don't implement the STLS
    extension
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

_log_level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)

BASE_URL = os.getenv("VAULT_CLIENT_TARGET_URL", "http://127.0.0.1:8765")


def _section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 60 - len(title)))


async def main() -> None:
    print("Deep Sky Vault — plain MCP discovery client")
    print(f"Target: {BASE_URL}/mcp  (no STLS, no encryption)")

    async with streamablehttp_client(
        url=f"{BASE_URL}/mcp",
    ) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:

            # ── initialize ────────────────────────────────────────────────────
            init_result = await session.initialize()
            _section("initialize")
            print(f"server   = {init_result.serverInfo.name} v{init_result.serverInfo.version}")
            print(f"protocol = {init_result.protocolVersion}")
            if init_result.instructions:
                print(f"instructions = {init_result.instructions[:90]}...")

            # ── Discovery: tools ─────────────────────────────────────────────
            _section("Discovery: tools")
            tools = (await session.list_tools()).tools
            for t in tools:
                print(f"  - {t.name}: {t.description}")

            # ── Discovery: resources ─────────────────────────────────────────
            _section("Discovery: resources")
            resources = (await session.list_resources()).resources
            for r in resources:
                print(f"  - {r.uri}: {r.description or r.name}")

            templates = (await session.list_resource_templates()).resourceTemplates
            for rt in templates:
                print(f"  - {rt.uriTemplate}: {rt.description or rt.name}")

            # ── Discovery: prompts ────────────────────────────────────────────
            _section("Discovery: prompts")
            prompts = (await session.list_prompts()).prompts
            for p in prompts:
                args = ", ".join(a.name for a in (p.arguments or []))
                print(f"  - {p.name}({args}): {p.description}")

            # ── Exercise the tools ───────────────────────────────────────────
            _section("Tool: list_stations")
            result = await session.call_tool("list_stations", {})
            print(result.content[0].text)

            _section("Tool: get_station_status('halcyon')")
            result = await session.call_tool("get_station_status", {"station_id": "halcyon"})
            print(result.content[0].text)

            _section("Tool: decode_transmission('kepler-9')")
            result = await session.call_tool("decode_transmission", {"station_id": "kepler-9"})
            print(f"Decoded message: {result.content[0].text}")

            _section("Tool: calculate_orbit(altitude_km=408)")
            result = await session.call_tool("calculate_orbit", {"altitude_km": 408})
            print(result.content[0].text)

            _section("Tool: signal_strength_check('vega-relay')")
            result = await session.call_tool("signal_strength_check", {"station_id": "vega-relay"})
            print(result.content[0].text)

            # ── Exercise the resources ───────────────────────────────────────
            _section("Resource: vault://manifest")
            manifest = await session.read_resource("vault://manifest")
            manifest_text = manifest.contents[0].text
            print(manifest_text)
            manifest_data = json.loads(manifest_text)
            assert manifest_data["station_count"] == 3, "Expected a manifest with 3 stations"

            _section("Resource: vault://station/halcyon (template URI)")
            detail = await session.read_resource("vault://station/halcyon")
            print(detail.contents[0].text)

            # ── Exercise the prompts ───────────────────────────────────────────
            _section("Prompt: mission_briefing(station_id='halcyon', urgency='urgent')")
            briefing = await session.get_prompt(
                "mission_briefing", {"station_id": "halcyon", "urgency": "urgent"}
            )
            for msg in briefing.messages:
                print(f"[{msg.role}] {msg.content.text}")

            _section("Prompt: fleet_summary(focus='crew morale')")
            summary_prompt = await session.get_prompt("fleet_summary", {"focus": "crew morale"})
            for msg in summary_prompt.messages:
                print(f"[{msg.role}] {msg.content.text}")

    _section("Done")
    print("All tools, resources, and prompts exercised successfully over /mcp.")
    print("Endpoint /mcp-stls was never touched by this client — fully isolated.")


if __name__ == "__main__":
    asyncio.run(main())