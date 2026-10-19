"""
vault_agent_test_lg.py
-----------------------
LangChain/LangGraph test agent against vault_server.py (Deep Sky Vault),
in two modes:

    plain    -> standard MultiServerMCPClient against /mcp, no encryption.
    --secure -> same MultiServerMCPClient, but with an STLS-encrypted
                httpx_client_factory injected into the connection config
                against /mcp-stls (handshake + AES-256-GCM traffic).

Mirrors vault_agent_test.py (ADK variant), same server, same tool set,
same plain/secure comparison — but here WITHOUT any dependency on
google-adk. Not by accident: unlike ADK, langchain-mcp-adapters
natively exposes httpx_client_factory on StreamableHttpConnection
(verified against the official docs:
https://reference.langchain.com/python/langchain-mcp-adapters/sessions/StreamableHttpConnection)
— no subclassing, no workaround. Just pass the factory produced by
stls_httpx_factory() (stls_mcp_transport.py, framework-agnostic) inside
the server's configuration dict. This is exactly the point raised
yesterday: STLS-MCP encryption must not drag ADK along as a
dependency — here we demonstrate it with a real client that never
touches it.

Running it:
    # Terminal 1 — the server (unchanged, start it first)
    python vault_server.py
    # or, for the PQC-encrypted channel:
    KEY_PROVIDER_NAME=pqc python vault_server.py

    # Terminal 2 — this script
    pip install langchain-mcp-adapters "langchain[google-genai]"
    export GOOGLE_API_KEY=...
    python vault_agent_test_lg.py                # plain, against /mcp
    python vault_agent_test_lg.py --secure        # PQC-encrypted, against /mcp-stls

Environment variables:
    VAULT_BASE_URL    base URL of the server (default: http://127.0.0.1:8765)
    STLS_CLIENT_ID    client_id for the handshake (default: vault-agent-test-lg)
    KEY_PROVIDER_NAME Provider for --secure when != pqc (default: pqc).
                       With "base"/"arqit_py" it resolves the session_key
                       from its own KeySessionProvider — it must match
                       the server's, otherwise the handshake fails on
                       the proof-of-possession (not a silent mismatch;
                       the same behavior already seen with stls-a2a and
                       with the ADK variant of this same test).
    GOOGLE_API_KEY    required by langchain_google_genai (google_genai:...).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

from stls_mcp import (
    perform_client_handshake,
    perform_pqc_client_handshake,
    stls_httpx_factory,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

MODEL = "google_genai:gemini-2.5-flash"
SERVER_KEY = "vault"  # arbitrary key in MultiServerMCPClient's config dict

SYSTEM_PROMPT = (
    "You are a fleet operations agent for the Deep Sky Vault. "
    "Use the available tools to answer questions about the fleet — "
    "never make up station data. Keep answers concise."
)

# Same round of prompts as the ADK variant, for a direct comparison.
TEST_PROMPTS = [
    "List all stations currently in the fleet.",
    "What's the detailed status of kepler-9?",
    "Decode the latest incoming transmission from vega-relay.",
    "Estimate the orbital period for a station at 850 km altitude.",
]

# Same env var names as the server (vault_server.py) — if you configured
# a custom path there (e.g. MCP_STLS_PATH=/vault/mcp-stls), the client
# must read the SAME variable to stay in sync. Replaces importing the
# fixed constant from stls_mcp (which isn't env-aware). They have
# nothing to do with the handshake, which always stays at the server
# root independently of these two — see build_client() and VAULT_BASE_URL.
MCP_PLAIN_PATH = os.getenv("MCP_PLAIN_PATH", "/mcp")
MCP_STLS_PATH = os.getenv("MCP_STLS_PATH", "/mcp-stls")

# Application header for AUTH_ENABLED=true on the server side — see the
# equivalent comment in vault_agent_test.py. None = no header sent.
VAULT_API_KEY = os.getenv("VAULT_API_KEY")


async def build_client(*, secure: bool, base_url: str) -> MultiServerMCPClient:
    """Builds the plain or encrypted MultiServerMCPClient depending on the --secure flag."""
    if not secure:
        logger.info("PLAIN client against %s%s", base_url, MCP_PLAIN_PATH)
        connection = {
            "transport": "http",
            "url": f"{base_url}{MCP_PLAIN_PATH}",
        }
        if VAULT_API_KEY:
            connection["headers"] = {"X-API-Key": VAULT_API_KEY}
        return MultiServerMCPClient({SERVER_KEY: connection})

    provider = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()
    client_id = os.getenv("STLS_CLIENT_ID", "vault-agent-test-lg")

    if provider == "pqc":
        session_id, transport = await perform_pqc_client_handshake(
            base_url, client_id=client_id,
        )
    else:
        # base/arqit_py: the session_key must be resolved from its own
        # KeySessionProvider BEFORE the handshake — exactly like
        # vault_server.py does to build its own SERVER provider. It must
        # be the same provider as the server's, or the handshake will
        # fail on the proof-of-possession (not a silent mismatch).
        #
        # Do NOT use KeySessionProviderFactory.resolve_if_awaitable()
        # here: it calls asyncio.run() internally, meant for synchronous
        # callers with no event loop already running. build_client() is
        # already async, running inside this script's
        # asyncio.run(main()) — a second, nested asyncio.run() raises a
        # RuntimeError ("cannot be called from a running event loop",
        # seen in the ADK variant of this same test).
        # getSessionKeyIdentifier()/getSessionKeyData() are always
        # coroutines by ABC contract (KeySessionProvider in
        # stls_core_providers_base.py), so they're awaited directly.
        from stls_core import KeySessionProviderFactory
        from stls_core.providers.stls_core_providers_base import KeySessionProvider

        key_session_provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            host="localhost",
            port=int(os.getenv("KEY_SESSION_PORT", "8091")),
        )
        kid = await key_session_provider.getSessionKeyIdentifier()
        session_key = await key_session_provider.getSessionKeyData(kid)
        session_id, transport = await perform_client_handshake(
            base_url, session_key, client_id=client_id,
        )

    logger.info(
        "ENCRYPTED client (provider=%s) against %s%s", provider, base_url, MCP_STLS_PATH
    )

    # No ADK dependency here: stls_httpx_factory() is pure httpx/stls_core,
    # and langchain-mcp-adapters accepts httpx_client_factory natively on
    # StreamableHttpConnection (NotRequired[McpHttpClientFactory | None]) —
    # unlike ADK, no subclassing/workaround needed.
    factory = stls_httpx_factory(session_id, transport, stls_path=MCP_STLS_PATH)

    return MultiServerMCPClient({
        SERVER_KEY: {
            "transport": "http",
            "url": f"{base_url}{MCP_STLS_PATH}",
            "httpx_client_factory": factory,
            **({"headers": {"X-API-Key": VAULT_API_KEY}} if VAULT_API_KEY else {}),
        }
    })


async def main() -> None:
    parser = argparse.ArgumentParser(description="Vault agent test — LangChain/LangGraph (plain/secure)")
    parser.add_argument(
        "--secure",
        action="store_true",
        help="Use the STLS encrypted channel (/mcp-stls) instead of plain (/mcp).",
    )
    args = parser.parse_args()

    base_url = os.getenv("VAULT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")

    print("\n" + "=" * 60)
    print(f"Vault Agent Test (LangChain/LangGraph) — {'ENCRYPTED (STLS)' if args.secure else 'PLAIN'} mode")
    print("=" * 60)
    print(f"  Server : {base_url}")
    print()

    client = await build_client(secure=args.secure, base_url=base_url)
    tools = await client.get_tools()
    logger.info("Tools loaded from server: %s", [t.name for t in tools])

    graph = create_agent(model=MODEL, tools=tools, system_prompt=SYSTEM_PROMPT)

    try:
        for i, prompt in enumerate(TEST_PROMPTS, start=1):
            print(f"── Prompt {i}/{len(TEST_PROMPTS)} " + "─" * 40)
            print(f"  👤 {prompt}")

            result = await graph.ainvoke({"messages": [{"role": "user", "content": prompt}]})
            final_message = result["messages"][-1]
            final_text = getattr(final_message, "content", None) or "(no final answer)"
            print(f"  🤖 {final_text}\n")
    finally:
        # MultiServerMCPClient is stateless by default (a new
        # ClientSession for every tool invocation, closed right after) —
        # there's no persistent connection to tear down like in the ADK
        # case. The defensive check is still useful if this ever moves
        # to an explicit, stateful client.session(...).
        aclose = getattr(client, "aclose", None) or getattr(client, "close", None)
        if aclose is not None:
            result = aclose()
            if asyncio.iscoroutine(result):
                await result

    print("=" * 60)
    print("Test completed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
