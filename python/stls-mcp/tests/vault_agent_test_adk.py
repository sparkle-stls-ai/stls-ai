"""
vault_agent_test.py
--------------------
ADK test agent against vault_server.py (Deep Sky Vault), in two modes:

    plain    -> standard MCPToolset against /mcp, no encryption.
    --secure -> STLSMCPToolset (stls_mcp_secure_toolset.py) against
                /mcp-stls, STLS handshake + AES-256-GCM encrypted traffic.

Mirrors server_context_test.py --secure (stls-a2a, earlier session):
same application agent, same set of tools exposed by the server, the
only difference is the transport used to reach it — useful for
comparing behavior and observed traffic between the two modes.

Running it:
    # Terminal 1 — the server (unchanged, start it first)
    python vault_server.py
    # or, for the PQC-encrypted channel:
    KEY_PROVIDER_NAME=pqc python vault_server.py

    # Terminal 2 — this script
    python vault_agent_test.py                # plain, against /mcp
    python vault_agent_test.py --secure        # PQC-encrypted, against /mcp-stls

Environment variables:
    VAULT_BASE_URL    base URL of the server — ALWAYS the process root
                      (e.g. "http://127.0.0.1:8765"), NEVER with the
                      MCP_PLAIN_PATH/MCP_STLS_PATH prefix even if you
                      configured them as custom on the server: the
                      handshake endpoint always lives at the server
                      root, independently of these two — the exact
                      same pattern already seen with stls-a2a's PQC
                      handshake. E.g. with MCP_STLS_PATH=/vault/mcp-stls
                      on the server, VAULT_BASE_URL stays
                      "http://host:port" (not ".../vault"), and the
                      handshake correctly ends up at
                      "http://host:port/stls/pqc/handshake".
    MCP_PLAIN_PATH    Path of the plain endpoint (default: /mcp). Must
                      match what's configured on the server.
    MCP_STLS_PATH     Path of the encrypted endpoint (default: /mcp-stls).
                      Must match what's configured on the server.
    STLS_CLIENT_ID    client_id for the handshake (default: vault-agent-test)
    KEY_PROVIDER_NAME Provider for --secure when != pqc (default: pqc).
                       With "base"/"arqit_py" it resolves the session_key
                       from its own KeySessionProvider, exactly like the
                       server does to generate its own — they must
                       match (the exact same issue seen yesterday with
                       stls-a2a: if client and server have different
                       providers, the handshake fails with an invalid
                       proof-of-possession, not silently).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams
from google.genai import types

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

MODEL_GEMINI_2_5_FLASH = "gemini-2.5-flash"

APP_NAME = "vault_agent_test"
USER_ID = "test-user"

# A round of prompts touching different tools, to validate that the
# agent actually calls the server (not just that the handshake succeeds).
TEST_PROMPTS = [
    "List all stations currently in the fleet.",
    "What's the detailed status of kepler-9?",
    "Decode the latest incoming transmission from vega-relay.",
    "Estimate the orbital period for a station at 850 km altitude.",
]

# Same env var names as the server (vault_server.py) — if you configured
# a custom path there (e.g. MCP_STLS_PATH=/vault/mcp-stls), the client
# must read the SAME variable to stay in sync. They have nothing to do
# with the handshake, which always stays at the server root
# independently of these two — see build_toolset() below and the
# comment on base_url.
MCP_PLAIN_PATH = os.getenv("MCP_PLAIN_PATH", "/mcp")
MCP_STLS_PATH = os.getenv("MCP_STLS_PATH", "/mcp-stls")

# Application header for AUTH_ENABLED=true on the server side
# (vault_server.py) — see stls_mcp_auth.AuthConfig / ApiKeyManager. None
# (VAULT_API_KEY unset) = no header sent, unchanged behavior if the
# server has auth disabled. Not needed for the handshake, which always
# stays open by design — only for the actual MCP calls.
VAULT_API_KEY = os.getenv("VAULT_API_KEY")


async def build_toolset(*, secure: bool, base_url: str) -> MCPToolset:
    """Builds the plain or encrypted toolset depending on the --secure flag."""
    if not secure:
        logger.info("PLAIN toolset against %s%s", base_url, MCP_PLAIN_PATH)
        return MCPToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=f"{base_url}{MCP_PLAIN_PATH}",
                timeout=30,
                headers={"X-API-Key": VAULT_API_KEY} if VAULT_API_KEY else None,
            ),
        )

    # Lazy import: only the --secure path needs stls_mcp_secure_toolset
    # (and therefore google-adk correctly resolved, liboqs if pqc, etc.)
    # — keeping it out of the plain path avoids introducing unnecessary
    # dependencies for anyone who just wants the plain test.
    from stls_mcp import create_stls_mcp_toolset

    provider = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()
    client_id = os.getenv("STLS_CLIENT_ID", "vault-agent-test")

    session_key = None
    if provider != "pqc":
        # base/arqit_py: the session_key must be resolved from its own
        # KeySessionProvider BEFORE the handshake — exactly like
        # vault_server.py does to build its own SERVER provider. It must
        # be the same provider as the server's, or the handshake will
        # fail on the proof-of-possession (not a silent mismatch).
        from stls_core import KeySessionProviderFactory
        from stls_core.providers.stls_core_providers_base import KeySessionProvider

        key_session_provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            host="localhost",
            port=int(os.getenv("KEY_SESSION_PORT", "8091")),
        )
        # Do NOT use KeySessionProviderFactory.resolve_if_awaitable() here:
        # it calls asyncio.run() internally, meant for synchronous callers
        # with no event loop already running (e.g. non-async scripts).
        # build_toolset() is already async, running inside this script's
        # asyncio.run(main()) — a second, nested asyncio.run() raises a
        # RuntimeError ("cannot be called from a running event loop").
        # getSessionKeyIdentifier()/getSessionKeyData() are always
        # coroutines by ABC contract (KeySessionProvider in
        # stls_core_providers_base.py), so they're awaited directly, no
        # need for the helper.
        kid = await key_session_provider.getSessionKeyIdentifier()
        session_key = await key_session_provider.getSessionKeyData(kid)

    logger.info(
        "ENCRYPTED toolset (provider=%s) against %s%s", provider, base_url, MCP_STLS_PATH
    )
    return await create_stls_mcp_toolset(
        base_url,
        client_id=client_id,
        provider=provider,
        session_key=session_key,
        stls_path=MCP_STLS_PATH,
        headers={"X-API-Key": VAULT_API_KEY} if VAULT_API_KEY else None,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Vault agent test (plain/secure)")
    parser.add_argument(
        "--secure",
        action="store_true",
        help="Use the STLS encrypted channel (/mcp-stls) instead of plain (/mcp).",
    )
    args = parser.parse_args()

    base_url = os.getenv("VAULT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")

    print("\n" + "=" * 60)
    print(f"Vault Agent Test — {'ENCRYPTED (STLS)' if args.secure else 'PLAIN'} mode")
    print("=" * 60)
    print(f"  Server : {base_url}")
    print()

    toolset = await build_toolset(secure=args.secure, base_url=base_url)

    agent = Agent(
        name="vault_operator",
        model=MODEL_GEMINI_2_5_FLASH,
        description="Fleet operations agent for the Deep Sky Vault.",
        instruction=(
            "You are a fleet operations agent for the Deep Sky Vault. "
            "Use the available tools to answer questions about the fleet — "
            "never make up station data. Keep answers concise."
        ),
        tools=[toolset],
    )

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session_service = runner.session_service
    session_id = f"session-{'secure' if args.secure else 'plain'}"
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )

    try:
        for i, prompt in enumerate(TEST_PROMPTS, start=1):
            print(f"── Prompt {i}/{len(TEST_PROMPTS)} " + "─" * 40)
            print(f"  👤 {prompt}")

            user_message = types.Content(role="user", parts=[types.Part(text=prompt)])
            final_text = "(no final answer)"
            async for event in runner.run_async(
                user_id=USER_ID, session_id=session_id, new_message=user_message
            ):
                if event.content and event.content.parts:
                    text = "".join(p.text or "" for p in event.content.parts)
                    if text.strip():
                        final_text = text
            print(f"  🤖 {final_text}\n")
    finally:
        # MCPToolset opens a persistent (Streamable HTTP) connection — it
        # must be closed explicitly, otherwise it stays attached to the
        # server even after the script ends. close() is async on all
        # verified ADK versions; defensive anyway in case it doesn't exist.
        close = getattr(toolset, "close", None)
        if close is not None:
            await close()

    print("=" * 60)
    print("Test completed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
