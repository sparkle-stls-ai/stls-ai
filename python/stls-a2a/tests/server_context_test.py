"""
server_context_test.py
-----------------------
Server di test per la verifica della propagazione del contesto utente.

Espone due agenti su porte diverse:
  - Agente B (porta 8002): diagnostica — risponde con user_id e profile ricevuti
  - Agente A (porta 8001): root — delega a B via ContextualRemoteA2aAgent

Avvio:
  # Terminale 1 — server B (va avviato per primo)
  # Con --secure su A serve KEY_PROVIDER_NAME=pqc qui, per esporre
  # l'endpoint di handshake /stls/pqc/handshake.
  PORT=8002 BASE_URL=http://localhost:8002 KEY_PROVIDER_NAME=pqc \
      python server_context_test.py --agent b

  # Terminale 2 — server A, delega PLAIN a B (comportamento di default)
  PORT=8001 BASE_URL=http://localhost:8001 python server_context_test.py --agent a

  # Terminale 2bis — server A, delega CIFRATA a B (/secure-a2a, handshake PQC)
  PORT=8001 BASE_URL=http://localhost:8001 python server_context_test.py --agent a --secure

Variabili d'ambiente:
  PORT          Porta di ascolto (default: 8001 per A, 8002 per B)
  BASE_URL      URL base dell'agente (usato per costruire l'AgentCard)
  API_KEY_A     API key richiesta per chiamare A (default: test-key-agent-a)
  API_KEY_B     API key richiesta per chiamare B (default: test-key-agent-b)
  BASE_URL_B    URL di B visto da A (default: http://localhost:8002)
  KEY_PROVIDER_NAME  Provider chiavi STLS (default: base — no JVM)
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
    Server B — agente di diagnostica.
    Espone agent_b su /agents/agent-b/secure-a2a.
    Protetto da API key statica (AuthConfig) per semplicità nel test.
    """
    from agent_context_test_b import agent_b

    api_key_b = os.getenv("API_KEY_B", "test-key-agent-b")
    port      = int(os.getenv("PORT", "8012"))
    base_url  = os.getenv("BASE_URL", f"http://localhost:{port}").rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Server B avviato su %s (port %d)", base_url, port)
        yield
        logger.info("Server B in shutdown")

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
    Server A — root agent che delega a B.

    secure=False (default): usa agent_context_test_a.agent_a — delega
        plain via ContextualRemoteA2aAgent (traffico /a2a in chiaro).
    secure=True: usa agent_context_test_a_secure.agent_a_secure — delega
        cifrata via ContextualSecureRemoteA2aAgent (traffico /secure-a2a,
        handshake PQC lazy verso B al primo utilizzo).

    NOTA: con secure=True, il SERVER B deve girare con
    KEY_PROVIDER_NAME=pqc per esporre l'endpoint di handshake
    (/stls/pqc/handshake) — altrimenti l'handshake di A verso B fallisce
    subito alla prima richiesta. Questo è indipendente dal KEY_PROVIDER_NAME
    con cui gira A stesso (che riguarda solo il /secure-a2a di A verso i
    SUOI chiamanti, non le chiamate di A verso B).
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
        logger.info("Server A avviato su %s (port %d)", base_url, port)
        yield
        logger.info("Server A in shutdown")

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
        help="Quale agente esporre: 'a' (root) o 'b' (diagnostica)",
    )
    parser.add_argument(
        "--secure",
        action="store_true",
        help=(
            "Solo per --agent a: usa ContextualSecureRemoteA2aAgent "
            "(delega cifrata via /secure-a2a con handshake PQC) invece "
            "di ContextualRemoteA2aAgent (delega plain via /a2a). "
            "Richiede che il server B giri con KEY_PROVIDER_NAME=pqc."
        ),
    )
    args = parser.parse_args()

    if args.secure and args.agent == "b":
        logger.warning(
            "--secure non ha effetto su --agent b: il flag sceglie solo "
            "come A delega a B. Assicurati invece che B stia girando con "
            "KEY_PROVIDER_NAME=pqc, altrimenti l'handshake di A fallirà."
        )

    if args.agent == "b":
        app  = build_app_b()
        port = int(os.getenv("PORT", 8012))
        label = "B (diagnostica)"
        path  = "/agents/agent-b/secure-a2a"
    else:
        app  = build_app_a(secure=args.secure)
        port = int(os.getenv("PORT", 8011))
        label = "A (root)" + (" — delega CIFRATA verso B" if args.secure else " — delega plain verso B")
        path  = "/agents/agent-a/secure-a2a"

    print("\n" + "=" * 60)
    print(f"Context Propagation Test — Agente {label}")
    print("=" * 60)
    print(f"  Endpoint cifrato : http://localhost:{port}{path}")
    print(f"  Endpoint plain   : http://localhost:{port}{path.replace('secure-', '')}")
    print(f"  Agent card       : http://localhost:{port}/.well-known/agent.json")
    print(f"  KEY_PROVIDER_NAME: {os.getenv('KEY_PROVIDER_NAME', 'base')}")
    if args.agent == "a":
        print(f"  BASE_URL_B       : {os.getenv('BASE_URL_B', 'http://localhost:8002')}")
        print(f"  Delega verso B   : {'CIFRATA (secure-a2a, handshake PQC)' if args.secure else 'PLAIN (a2a)'}")
        if args.secure and os.getenv("KEY_PROVIDER_NAME", "base") != "pqc":
            print(
                "  ATTENZIONE       : --secure richiede che B giri con "
                "KEY_PROVIDER_NAME=pqc (verifica sul processo di B, non su questo)."
            )
    print()

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()