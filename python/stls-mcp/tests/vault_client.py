"""
vault_client.py
----------------
Discovery client for the "Deep Sky Vault" example server
(vault_server.py). Performs the STLS handshake, then a full MCP session
through the encrypted transport (/mcp-stls): discovery of tools,
resources and prompts, and exercising each at least once.

Usage:
    # terminal 1
    python tests/vault_server.py

    # terminal 2
    python tests/vault_client.py

Also meant as a reading reference: shows the complete
"handshake -> stls_httpx_factory -> streamablehttp_client -> ClientSession"
pattern that any stls-mcp-based client application should follow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from stls_core import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider
from stls_mcp import perform_client_handshake, perform_pqc_client_handshake, stls_httpx_factory

load_dotenv()

# Without explicit configuration, the Arqit peering INFO/DEBUG logs
# (device authentication, bilocation/P2P negotiation) stay silent —
# useful in production, but makes it impossible to diagnose peering
# issues from here. INFO level by default; pass --debug for DEBUG.
import sys
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
    print("Deep Sky Vault — discovery client")
    print(f"Target: {BASE_URL}")

    # ── STLS handshake (standard or PQC depending on KEY_PROVIDER_NAME) ─────
    # PQC: no pre-shared key — keypairs are generated on the fly and the
    #      session_key is derived via KEM (X25519 + ML-KEM-768 + HKDF).
    # Standard (base/arqit_py): the session_key is resolved from the local
    #      provider (fixed key or SKA-Platform) and the HMAC proof verifies
    #      its possession.
    _section("Handshake")
    _provider_name = os.getenv("KEY_PROVIDER_NAME", "base").strip().lower()

    if _provider_name == "pqc":
        print(f"Provider: pqc — X25519 + ML-KEM-768, no pre-shared key")
        session_id, transport = await perform_pqc_client_handshake(
            BASE_URL, client_id="vault-demo-client",
        )
    else:
        # Resolve the session key from the local provider (base or arqit_py)
        _key_session_port = int(os.getenv("KEY_SESSION_PORT", "8091"))
        client_provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            port=_key_session_port,
        )
        kid = await client_provider.getSessionKeyIdentifier()
        session_key = await client_provider.getSessionKeyData(kid)
        print(f"Provider: {_provider_name} — kid={kid}")
        session_id, transport = await perform_client_handshake(
            BASE_URL, session_key, client_id="vault-demo-client",
        )
    print(f"session_id = {session_id}")
    print("Proof of key possession verified — channel is ready.")

    factory = stls_httpx_factory(session_id, transport)

    async with streamablehttp_client(
        url=f"{BASE_URL}/mcp-stls",
        httpx_client_factory=factory,
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
    print("All tools, resources, and prompts exercised successfully over /mcp-stls.")
    print("Plain /mcp was never touched by this client — fully isolated.")


if __name__ == "__main__":
    asyncio.run(main())