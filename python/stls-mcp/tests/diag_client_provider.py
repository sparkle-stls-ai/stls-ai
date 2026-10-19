"""
diag_client_provider.py
------------------------
Minimal diagnostic script to isolate why Arqit peering on the CLIENT
side doesn't seem to start in vault_client.py. Runs EXACTLY the same
sequence (KeySessionProviderFactory -> getSessionKeyIdentifier ->
getSessionKeyData), but with explicit logging at every step and without
the rest of the client (HTTP handshake, MCP session, etc.) around it.

Usage:
    cd ~/VSProjects/stls-ai-java-sdk/python/stls-mcp
    uv run python diag_client_provider.py
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)

print("=== CLIENT PROVIDER DIAGNOSTICS ===")
print(f"KEY_PROVIDER_NAME = {os.getenv('KEY_PROVIDER_NAME')!r}")
print(f"KEY_SESSION_PORT  = {os.getenv('KEY_SESSION_PORT')!r}")
print(f"ARQIT_PY_CLIENT_CONFIG = {os.getenv('ARQIT_PY_CLIENT_CONFIG')!r}")
print(f"ARQIT_PY_TRANSPORT     = {os.getenv('ARQIT_PY_TRANSPORT')!r}")
print()


async def main() -> None:
    from stls_core import KeySessionProviderFactory
    from stls_core.providers.stls_core_providers_base import KeySessionProvider

    print("STEP 1: KeySessionProviderFactory().get_session_provider(CLIENT) ...")
    port = int(os.getenv("KEY_SESSION_PORT", "8091"))

    factory = KeySessionProviderFactory()
    print(f"  factory id = {id(factory)}")

    client_provider = factory.get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
        port=port,
    )
    print(f"  client_provider = {client_provider!r} (type={type(client_provider).__name__})")
    print("STEP 1: OK, get_session_provider() returned (non-blocking, as expected)")
    print()

    print("STEP 2: client_provider.getSessionKeyIdentifier() — THIS should block")
    print("        until peering completes (or the 30s timeout)...")
    kid = await client_provider.getSessionKeyIdentifier()
    print(f"STEP 2: OK, kid = {kid!r}")
    print()

    print("STEP 3: client_provider.getSessionKeyData(kid) ...")
    key = await client_provider.getSessionKeyData(kid)
    print(f"STEP 3: OK, key length = {len(key)} bytes")
    print()

    print("=== DIAGNOSTICS COMPLETED SUCCESSFULLY ===")


if __name__ == "__main__":
    asyncio.run(main())
