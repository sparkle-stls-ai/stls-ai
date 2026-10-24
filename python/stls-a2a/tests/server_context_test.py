"""
server_context_test.py
-----------------------
Test server for verifying user context propagation.

Exposes two agents on different ports:
  - Agent B (port 8002): diagnostics — replies with the received user_id and profile
  - Agent A (port 8001): root — delegates to B via ContextualRemoteA2aAgent

Startup:
  # Terminal 1 — server B (must be started first)
  # With --secure on A, this needs KEY_PROVIDER_NAME=pqc here, to expose
  # the /stls/pqc/handshake endpoint.
  PORT=8002 BASE_URL=http://localhost:8002 KEY_PROVIDER_NAME=pqc \
      python server_context_test.py --agent b

  # Terminal 2 — server A, PLAIN delegation to B (default behavior)
  PORT=8001 BASE_URL=http://localhost:8001 python server_context_test.py --agent a

  # Terminal 2bis — server A, ENCRYPTED delegation to B (/secure-a2a, PQC handshake)
  PORT=8001 BASE_URL=http://localhost:8001 python server_context_test.py --agent a --secure

Environment variables:
  PORT          Listen port (default: 8001 for A, 8002 for B)
  BASE_URL      Base URL of the agent (used to build the AgentCard)
  API_KEY_A     API key required to call A (default: test-key-agent-a)
  API_KEY_B     API key required to call B (default: test-key-agent-b)
  BASE_URL_B    URL of B as seen by A (default: http://localhost:8002)
  KEY_PROVIDER_NAME  STLS key provider (default: base — no JVM)
"""

from __future__ import annotations

import argparse
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
    AuthConfig,
)


def build_app_b() -> FastAPI:
    """
    Server B — diagnostic agent.
    Exposes agent_b on /agents/agent-b/secure-a2a.
    Protected by a static API key (AuthConfig) for simplicity in the test.
    """
    from agent_context_test_b import agent_b

    api_key_b = os.getenv("API_KEY_B", "test-key-agent-b")
    port      = int(os.getenv("PORT", "8012"))
    base_url  = os.getenv("BASE_URL", f"http://localhost:{port}").rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Server B started on %s (port %d)", base_url, port)
        yield
        logger.info("Server B shutting down")

    app = FastAPI(title="Context Test — Agent B", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    stls_port_b = int(os.getenv("STLS_PORT_B", "8092"))
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=stls_port_b,
    )

    create_secure_a2a_endpoint(
        app=app,
        agent=agent_b,
        key_session_provider=key_session_provider,
        path="/agents/agent-b/secure-a2a",
        base_url=base_url,
        framework="google-adk",
        auth=AuthConfig(api_keys={api_key_b}),
    )

    return app


def build_app_a(secure: bool = False) -> FastAPI:
    """
    Server A — root agent that delegates to B.

    secure=False (default): uses agent_context_test_a.agent_a — plain
        delegation via ContextualRemoteA2aAgent (unencrypted /a2a traffic).
    secure=True: uses agent_context_test_a_secure.agent_a_secure —
        encrypted delegation via ContextualSecureRemoteA2aAgent
        (/secure-a2a traffic, lazy PQC handshake towards B on first use).

    NOTE: with secure=True, SERVER B must run with
    KEY_PROVIDER_NAME=pqc to expose the handshake endpoint
    (/stls/pqc/handshake) — otherwise A's handshake towards B fails
    immediately on the first request. This is independent of the
    KEY_PROVIDER_NAME A itself runs with (which only concerns A's own
    /secure-a2a towards ITS callers, not A's calls towards B).
    """
    if secure:
        from agent_context_test_a_secure import agent_a_secure as agent_a
    else:
        from agent_context_test_a import agent_a

    api_key_a = os.getenv("API_KEY_A", "test-key-agent-a")
    port      = int(os.getenv("PORT", "8011"))
    base_url  = os.getenv("BASE_URL", f"http://localhost:{port}").rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Server A started on %s (port %d)", base_url, port)
        yield
        logger.info("Server A shutting down")

    app = FastAPI(title="Context Test — Agent A", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    stls_port_a = int(os.getenv("STLS_PORT_A", "8091"))
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=stls_port_a,
    )

    create_secure_a2a_endpoint(
        app=app,
        agent=agent_a,
        key_session_provider=key_session_provider,
        path="/agents/agent-a/secure-a2a",
        base_url=base_url,
        framework="google-adk",
        auth=AuthConfig(api_keys={api_key_a}),
    )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Context propagation test server")
    parser.add_argument(
        "--agent",
        choices=["a", "b"],
        required=True,
        help="Which agent to expose: 'a' (root) or 'b' (diagnostics)",
    )
    parser.add_argument(
        "--secure",
        action="store_true",
        help=(
            "Only for --agent a: use ContextualSecureRemoteA2aAgent "
            "(encrypted delegation via /secure-a2a with PQC handshake) "
            "instead of ContextualRemoteA2aAgent (plain delegation via /a2a). "
            "Requires server B to run with KEY_PROVIDER_NAME=pqc."
        ),
    )
    args = parser.parse_args()

    if args.secure and args.agent == "b":
        logger.warning(
            "--secure has no effect on --agent b: the flag only chooses "
            "how A delegates to B. Instead make sure B is running with "
            "KEY_PROVIDER_NAME=pqc, otherwise A's handshake will fail."
        )

    if args.agent == "b":
        app  = build_app_b()
        port = int(os.getenv("PORT", 8012))
        label = "B (diagnostics)"
        path  = "/agents/agent-b/secure-a2a"
    else:
        app  = build_app_a(secure=args.secure)
        port = int(os.getenv("PORT", 8011))
        label = "A (root)" + (" — ENCRYPTED delegation to B" if args.secure else " — plain delegation to B")
        path  = "/agents/agent-a/secure-a2a"

    print("\n" + "=" * 60)
    print(f"Context Propagation Test — Agent {label}")
    print("=" * 60)
    print(f"  Encrypted endpoint: http://localhost:{port}{path}")
    print(f"  Plain endpoint    : http://localhost:{port}{path.replace('secure-', '')}")
    print(f"  Agent card        : http://localhost:{port}/.well-known/agent.json")
    print(f"  KEY_PROVIDER_NAME : {os.getenv('KEY_PROVIDER_NAME', 'base')}")
    if args.agent == "a":
        print(f"  BASE_URL_B        : {os.getenv('BASE_URL_B', 'http://localhost:8002')}")
        print(f"  Delegation to B   : {'ENCRYPTED (secure-a2a, PQC handshake)' if args.secure else 'PLAIN (a2a)'}")
        if args.secure and os.getenv("KEY_PROVIDER_NAME", "base") != "pqc":
            print(
                "  WARNING           : --secure requires B to run with "
                "KEY_PROVIDER_NAME=pqc (check B's own process, not this one)."
            )
    print()

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()