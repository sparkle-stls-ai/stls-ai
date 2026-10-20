"""
run_identity_bridge.py
------------------------
Fa girare l'Identity Bridge (DID Document + status list pubblici) come
server HTTP locale — stesso principio di agent_identity.py: file
esterno al package stls-identity, importa solo la sua API pubblica.

Indipendente da agent_identity.py: legge la propria configurazione da
.env.identity_bridge, non dal .env generico — così i due processi
(agente conversazionale, server bridge) restano separabili, ognuno col
proprio file di configurazione, anche se girano nella stessa directory
o nello stesso repo.

NOTA: la firma esatta di create_identity_bridge_router() qui sotto è
ricostruita dalla documentazione di progetto, non riverificata contro
il sorgente reale (perso in un reset del sandbox) — da confermare
contro stls_id_bridge.py prima di considerarlo definitivo.

Uso:
    uv run python run_identity_bridge.py
    # oppure, con reload per sviluppo:
    uv run uvicorn run_identity_bridge:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

from dotenv import load_dotenv
load_dotenv(".env.identity_bridge")

import redis.asyncio as redis_asyncio
import uvicorn
from fastapi import FastAPI

from stls_identity import (
    IdentityKeystoreFactory,
    PostgresDIDDocumentStore,
    PostgresStatusListCacheStore,
    StatusListCredential,
    StatusListManager,
    bootstrap_signing_key,
    build_did_document,
    create_identity_bridge_router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Stesse env vars di tools_identity.py — nessuna nuova convenzione introdotta,
# tranne le due per il refresher (in fondo a questo blocco).
_GOVERNING_AUTHORITY_DID = os.environ.get("GOVERNING_AUTHORITY_DID", "did:web:sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_HOST = os.environ.get("GOVERNING_AUTHORITY_HOST", "sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_AGENT_ID = os.environ.get("GOVERNING_AUTHORITY_AGENT_ID", "governing-authority")
_IDENTITY_AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "sofia-authority")
_STATUS_LIST_PUBLISH_URL = os.environ.get(
    "STATUS_LIST_PUBLISH_URL", "https://sofia.sparkle.example.com/identity/status-list"
)

# Deve stare SOTTO l'MRI (5 minuti nel codice, verifier.py) — altrimenti un
# verificatore esterno che legge questa cache può trovarla già stale secondo
# le sue stesse regole, anche se qui l'abbiamo appena ripubblicata.
_REFRESH_INTERVAL_SECONDS = int(os.environ.get("STATUS_LIST_REFRESH_INTERVAL_SECONDS", "60"))

did_store = PostgresDIDDocumentStore(
    dsn=os.environ.get("IDENTITY_PG_DSN", ""),
    table=os.environ.get("IDENTITY_PG_DID_TABLE"),
)
status_cache_store = PostgresStatusListCacheStore(
    dsn=os.environ.get("IDENTITY_PG_DSN", ""),
    table=os.environ.get("IDENTITY_PG_STATUS_LIST_TABLE"),
)

# Stessa Redis/authority_id di tools_identity.py — DEVE essere la stessa
# bitstring che IdentityManager.revoke() aggiorna, altrimenti il bridge
# pubblicherebbe lo stato di revoca di un'altra Authority.
#
# decode_responses=False QUI È CRITICO: la bitstring è dato binario
# grezzo (scritto con SETBIT), non testo. Con decode_responses=True,
# GET la restituisce come str, e StatusListManager.publish() la passa a
# gzip.compress() — che si aspetta bytes, non str — sollevando
# TypeError ad ogni ciclo di pubblicazione (bug reale, trovato in log:
# "TypeError: a bytes-like object is required, not 'str'").
_status_list_redis = redis_asyncio.from_url(
    os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
    decode_responses=False,
)
status_list_manager = StatusListManager(
    redis_client=_status_list_redis, authority_id=_IDENTITY_AUTHORITY_ID
)

router = create_identity_bridge_router(
    did_store=did_store,
    status_cache_store=status_cache_store,
    authority_id=_IDENTITY_AUTHORITY_ID,
    authority_agent_id=_GOVERNING_AUTHORITY_AGENT_ID,
)

app = FastAPI(title="STLS Identity Bridge", version="0.1.0")
app.include_router(router)

_refresher_task: asyncio.Task | None = None


async def _publish_once() -> None:
    """Legge la bitstring corrente da Redis (StatusListManager.publish(),
    compressa+incapsulata), la scrive nella cache Postgres che il bridge
    serve. Senza questo, /identity/status-list resta vuoto per sempre —
    IdentityManager.revoke() aggiorna solo il bit su Redis, non pubblica
    da solo nulla verso l'esterno."""
    encoded_list, valid_from = await status_list_manager.publish()
    credential = StatusListCredential(
        id=_STATUS_LIST_PUBLISH_URL,
        issuer=_GOVERNING_AUTHORITY_DID,
        valid_from=valid_from,
        encoded_list=encoded_list,
    )
    await status_cache_store.put(_IDENTITY_AUTHORITY_ID, credential)
    logger.info("status list pubblicata (valid_from=%s)", valid_from.isoformat())


async def _refresh_status_list_periodically() -> None:
    while True:
        try:
            await _publish_once()
        except Exception:
            logger.exception("pubblicazione periodica della status list fallita, riprovo al prossimo giro")
        await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)


@app.on_event("startup")
async def _ensure_schema() -> None:
    """Crea le tabelle se non esistono — stesso self-bootstrap di
    tools_identity.py, nessuna migrazione manuale richiesta."""
    pool = await did_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(did_store.get_ddl())

    pool2 = await status_cache_store._get_pool()
    async with pool2.acquire() as conn:
        await conn.execute(status_cache_store.get_ddl())

    # Il DID Document dell'AUTHORITY stessa — questo processo è
    # indipendente da agent_identity.py (può partire per primo, o da
    # solo), quindi non può assumere che qualcun altro l'abbia già
    # pubblicato. Stesso IDENTITY_KEYSTORE_BACKEND del .env dell'agente
    # -> bootstrap_signing_key è idempotente (get-or-create): se
    # l'agente ha già girato, questa chiamata recupera la STESSA chiave,
    # non ne genera una seconda. Senza questo passo, GET
    # /.well-known/did.json risponde "authority DID Document not
    # bootstrapped yet" per sempre — non un errore transitorio.
    keystore = IdentityKeystoreFactory.build()
    _, authority_pub, _ = bootstrap_signing_key(_GOVERNING_AUTHORITY_AGENT_ID, keystore)
    authority_document = build_did_document(
        host=_GOVERNING_AUTHORITY_HOST,
        agent_path_segments=[],  # DID puro (did:web:<host>), non un "agente" chiamato per caso governing-authority
        governing_authority_did=_GOVERNING_AUTHORITY_DID,  # auto-referenziale: root of trust, controller = se stessa
        signing_public_key=authority_pub,
    )
    await did_store.put(_GOVERNING_AUTHORITY_AGENT_ID, authority_document)

    # Pubblicazione immediata all'avvio — senza questa, /identity/status-list
    # risponderebbe "not published yet" fino al primo giro del task periodico.
    await _publish_once()

    global _refresher_task
    _refresher_task = asyncio.create_task(_refresh_status_list_periodically())

    logger.info(
        "Identity Bridge pronto — DID store, status cache store, DID Document "
        "dell'Authority e refresher (ogni %ss) inizializzati", _REFRESH_INTERVAL_SECONDS,
    )


@app.on_event("shutdown")
async def _stop_refresher() -> None:
    if _refresher_task is not None:
        _refresher_task.cancel()
    await _status_list_redis.aclose()


if __name__ == "__main__":
    port = int(os.environ.get("IDENTITY_BRIDGE_PORT", "8000"))
    logger.info("Avvio Identity Bridge su http://localhost:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)