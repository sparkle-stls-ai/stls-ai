"""
run_trust_registry.py
------------------------
Espone il Trust Anchor Registry via HTTP — Sparkle come anchor
operator della federazione (Opzione A: centralizzato). Altre
piattaforme (Operatore B, C, ...) leggono da qui quali Governing
Authority sono riconosciute e le loro chiavi correnti.

Indipendente da agent_identity.py e da run_identity_bridge.py: propria
configurazione (.env.trust_registry), proprio processo. Stesso
principio degli altri due file: esterno al package stls-identity,
importa solo la sua API pubblica.

Più semplice del bridge dell'identità — nessun task periodico di
pubblicazione: qui non c'è una bitstring da comprimere e cachare, il
router legge Postgres direttamente a ogni richiesta.

Nota: create_trust_registry_router(store=...) è confermato funzionante
per davvero — il router si costruisce ed espone endpoint (verificato
via /docs). L'unica cosa che avevo sbagliato inizialmente erano i path
assunti (/trust-anchors invece dei reali /trust-registry/anchors,
sotto) — corretti qui sotto dopo verifica diretta.

Endpoint reali (confermati via /docs, non più ipotizzati):
    GET /trust-registry/anchors               — elenco Authority riconosciute
    GET /trust-registry/anchors/{authority_id} — una specifica Authority

Uso:
    uv run python run_trust_registry.py
    # oppure, con reload per sviluppo:
    uv run uvicorn run_trust_registry:app --reload --port 8100
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
load_dotenv(".env.trust_registry")

import uvicorn
from fastapi import FastAPI

from stls_identity import (
    PostgresTrustAnchorRegistryStore,
    create_trust_registry_router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

trust_registry_store = PostgresTrustAnchorRegistryStore(
    dsn=os.environ.get("IDENTITY_PG_DSN", ""),
    table=os.environ.get("TRUST_REGISTRY_PG_TABLE"),
)

router = create_trust_registry_router(store=trust_registry_store)

app = FastAPI(title="STLS Trust Anchor Registry", version="0.1.0")
app.include_router(router)


@app.on_event("startup")
async def _ensure_schema() -> None:
    """Crea la tabella se non esiste — stesso self-bootstrap degli
    altri due processi, nessuna migrazione manuale richiesta. Stessa
    tabella che onboard_trust_anchor/list_trust_anchors/
    revoke_trust_anchor in tools_identity.py scrivono — questo processo
    legge solo, non scrive mai."""
    pool = await trust_registry_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(trust_registry_store.get_ddl())
    logger.info("Trust Anchor Registry pronto — sola lettura, scrittura via agent_identity.py")


if __name__ == "__main__":
    port = int(os.environ.get("TRUST_REGISTRY_PORT", "8100"))
    logger.info("Avvio Trust Anchor Registry su http://localhost:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)