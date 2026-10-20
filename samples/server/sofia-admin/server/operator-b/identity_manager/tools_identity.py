"""
tools_identity.py
------------------
Tools per sofia_identity_agent.

Gestisce il ciclo di vita delle identità (DID + Identity Credential)
degli agenti Sofia — stesso namespace concettuale di tools_redis.py
(stls:identity:*, tabelle Postgres stls_identity_*), stesso stile:
funzioni piatte a livello di modulo, non closure su un oggetto
dependency-injection.

Differenza rispetto a tools_redis.py: lì ogni funzione apre/chiude una
connessione Redis "usa e getta" ad ogni chiamata (client Redis sync,
economico da ricreare). Qui le risorse (pool Postgres, keystore delle
chiavi, IdentityManager) sono costruite UNA VOLTA e cachate a livello
di modulo — ricrearle ad ogni chiamata romperebbe l'idempotenza del
bootstrap (una nuova InMemoryIdentityKeystore ad ogni chiamata
dimenticherebbe la chiave appena generata) e sprecherebbe una
handshake asyncpg completa per ogni tool call.

Non fa MAI l'atto di delega: nessuna funzione qui chiama
DelegationManager. Assegnare il ruolo "grantor" a un'identità è
provisioning (issue_identity_credential_for_agent(roles=["grantor"])),
non l'atto di delegare — quello resta dell'agente che detiene il
ruolo, non di questo tool.

Env vars attese (stessi nomi di .env.example nel package stls-identity):
  IDENTITY_KEYSTORE_BACKEND       memory (default) | redis
  IDENTITY_KEYSTORE_REDIS_URL     redis://localhost:6379
  IDENTITY_KEYSTORE_REDIS_NAMESPACE  stls:identity:signingkey
  IDENTITY_PG_DSN                 DSN asyncpg per il DID store
  IDENTITY_PG_DID_TABLE           stls_identity_did_documents (default)
  STATUS_LIST_REDIS_URL           redis://localhost:6379 (usata anche dalla credential cache)
  IDENTITY_AUTHORITY_ID           sofia-authority (default)
  GOVERNING_AUTHORITY_DID         did:web:sofia.sparkle.example.com
  GOVERNING_AUTHORITY_HOST        sofia.sparkle.example.com
  STATUS_LIST_PUBLISH_URL         URL pubblico dello status list
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import logging
import os

import httpx
import redis.asyncio as redis_asyncio
from base58 import b58decode
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from stls_identity import (
    CredentialVerificationError,
    IdentityKeystoreFactory,
    IdentityManager,
    NetworkAuthorityCredential,
    OperationalCredential,
    PostgresDIDDocumentStore,
    PostgresTrustAnchorRegistryStore,
    Sentinel,
    StatusListManager,
    TrustAnchorEntry,
    TrustAnchorRegistryClient,
    bootstrap_signing_key,
    build_did_document,
)

logger = logging.getLogger(__name__)


# ── Config / risorse cachate a livello di modulo ────────────────────────────────
#
# Costruite alla PRIMA chiamata a un tool, poi riusate — non ricreate ad
# ogni invocazione. _deps() è idempotente: chiamarla più volte
# restituisce sempre lo stesso oggetto.

_GOVERNING_AUTHORITY_DID = os.environ.get("GOVERNING_AUTHORITY_DID", "did:web:sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_HOST = os.environ.get("GOVERNING_AUTHORITY_HOST", "sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_AGENT_ID = os.environ.get("GOVERNING_AUTHORITY_AGENT_ID", "governing-authority")
_IDENTITY_AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "sofia-authority")
_STATUS_LIST_PUBLISH_URL = os.environ.get(
    "STATUS_LIST_PUBLISH_URL", "https://sofia.sparkle.example.com/identity/status-list"
)
_AUTHORITY_VM_ID = f"{_GOVERNING_AUTHORITY_DID}#key-1"

# Log di emissione/revoca — SOLO audit, mai consultato dal path di
# verifica (validate_agent_identity_credential non lo tocca). Esiste
# per rispondere "a chi avevamo emesso l'indice N della status list",
# non per decidere se una credenziale è valida — quella resta sempre e
# solo firma + status list, mai questa tabella. Non fa parte del
# package stls-identity: il principio "nessun registro centrale delle
# credenziali emesse" è del protocollo, questo è solo un log operativo
# di chi lo gestisce.
_CREDENTIAL_LOG_TABLE = os.environ.get("IDENTITY_PG_CREDENTIAL_LOG_TABLE", "sf_identity_credential_log")

_CREDENTIAL_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS {table} (
        credential_id       TEXT PRIMARY KEY,
        agent_id            TEXT NOT NULL,
        status_list_index   INT NOT NULL,
        issued_at           TIMESTAMPTZ NOT NULL,
        valid_until          TIMESTAMPTZ,
        roles                JSONB,
        principal_ref        TEXT,
        revoked_at           TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS {table}_status_list_index_idx ON {table} (status_list_index);
    CREATE INDEX IF NOT EXISTS {table}_agent_id_idx ON {table} (agent_id);
"""
# Namespace della cache delle credenziali — stesso Redis di status list e
# keystore, stesso perimetro di fiducia: chi può già leggere la chiave
# privata di un agente è già dentro il perimetro giusto per leggere anche
# la sua credenziale (che finirà comunque presentata pubblicamente ad
# ogni chiamata — è meno sensibile della chiave, non di più).
_CRED_KEY_PREFIX = "stls:identity:credential"        # slot singolo, Identity Credential
_OPCRED_KEY_PREFIX = "stls:identity:opcred"           # collezione, Operational Credential
_OPCRED_INDEX_PREFIX = "stls:identity:opcred_index"   # sorted set, score = validUntil epoch


class _Deps:
    """Bundle di risorse condivise, costruito una sola volta. Non è un
    dataclass esposto pubblicamente — vive come dettaglio implementativo
    del modulo, coerente con lo stile "funzioni piatte" di tools_redis.py."""

    __slots__ = (
        "keystore", "did_store", "status_list_manager", "identity_manager",
        "sentinel", "cache_redis", "pg_pool", "credential_log_table",
        "trust_registry_store", "trust_registry_client",
    )

    def __init__(
        self, keystore, did_store, status_list_manager, identity_manager,
        sentinel, cache_redis, pg_pool, credential_log_table, trust_registry_store,
        trust_registry_client,
    ):
        self.keystore = keystore
        self.did_store = did_store
        self.status_list_manager = status_list_manager
        self.identity_manager = identity_manager
        self.sentinel = sentinel
        self.cache_redis = cache_redis
        self.pg_pool = pg_pool
        self.credential_log_table = credential_log_table
        self.trust_registry_store = trust_registry_store
        self.trust_registry_client = trust_registry_client


_deps_singleton: _Deps | None = None


async def _noop_audit_hook(entry) -> None:
    pass


async def _deps() -> _Deps:
    global _deps_singleton
    if _deps_singleton is not None:
        return _deps_singleton

    keystore = IdentityKeystoreFactory.build()

    authority_key, authority_pub, _ = bootstrap_signing_key(
        _GOVERNING_AUTHORITY_AGENT_ID, keystore
    )

    did_store = PostgresDIDDocumentStore(
        dsn=os.environ.get("IDENTITY_PG_DSN", ""),
        table=os.environ.get("IDENTITY_PG_DID_TABLE"),  # None -> default del package se non impostata
    )
    pool = await did_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(did_store.get_ddl())
        # Stesso pool di did_store — stesso database (IDENTITY_PG_DSN),
        # tabella separata. Nessuna seconda connessione da gestire.
        await conn.execute(_CREDENTIAL_LOG_DDL.format(table=_CREDENTIAL_LOG_TABLE))

    # Il DID Document dell'AUTHORITY stessa — diverso da quello di un
    # agente qualunque, per due motivi:
    #   1. agent_path_segments=[] -> il DID risultante è
    #      did:web:<host> puro, senza suffisso ":agents:...", perché
    #      è quello che GOVERNING_AUTHORITY_DID/_AUTHORITY_VM_ID si
    #      aspettano davvero (non un DID "agente" chiamato per caso
    #      "governing-authority").
    #   2. governing_authority_did=_GOVERNING_AUTHORITY_DID (se stessa)
    #      -> controller è auto-referenziale: l'Authority è root of
    #      trust, non ha un'Authority sopra di sé che la controlli,
    #      a differenza di ogni agente (il cui controller è invece
    #      l'Authority).
    # Pubblicato sotto la chiave di storage _GOVERNING_AUTHORITY_AGENT_ID
    # — è quella che il bridge legge per GET /.well-known/did.json.
    # Senza questo passo, quell'endpoint risponde "authority DID
    # Document not bootstrapped yet" per sempre — non è un errore
    # transitorio, nessuno lo pubblica mai da solo.
    authority_document = build_did_document(
        host=_GOVERNING_AUTHORITY_HOST,
        agent_path_segments=[],
        governing_authority_did=_GOVERNING_AUTHORITY_DID,
        signing_public_key=authority_pub,
    )
    await did_store.put(_GOVERNING_AUTHORITY_AGENT_ID, authority_document)

    # Trust Anchor Registry — Sparkle come anchor operator (Opzione A,
    # centralizzato). Store completo del package (storia delle chiavi
    # inclusa) — qui esposto solo con le operazioni CRUD di base
    # (onboard/list/revoke); resolve_historical() e la verifica di
    # credenziali esterne restano fuori scope per ora.
    trust_registry_store = PostgresTrustAnchorRegistryStore(
        dsn=os.environ.get("IDENTITY_PG_DSN", ""),
        table=os.environ.get("TRUST_REGISTRY_PG_TABLE"),
    )
    trust_pool = await trust_registry_store._get_pool()
    async with trust_pool.acquire() as conn:
        await conn.execute(trust_registry_store.get_ddl())

    # Client di lettura del registro — usato solo per risolvere la
    # chiave di un'Authority esterna in validate_external_identity_credential().
    # Distinto dallo store grezzo sopra (quello serve onboard/list/revoke,
    # scritture dirette; questo serve una cache con refresh esplicito,
    # pensata per la verifica).
    trust_registry_client = TrustAnchorRegistryClient(trust_registry_store)

    # Due client Redis DISTINTI, deliberatamente — non uno condiviso.
    #
    # status_redis: decode_responses=False. La bitstring di revoca è
    # dato binario grezzo (SETBIT/GETBIT). Con decode_responses=True,
    # StatusListManager.publish() riceverebbe una str da GET e la
    # passerebbe a gzip.compress(), che si aspetta bytes — TypeError ad
    # ogni pubblicazione (bug reale, trovato mentre testavamo il bridge:
    # "TypeError: a bytes-like object is required, not 'str'"). revoke()/
    # is_revoked()/allocate_index() non ne risentono (SETBIT/GETBIT/INCR
    # restituiscono sempre interi, mai stringhe) — per questo il bug è
    # rimasto silente qui e si è visto solo nel bridge, che chiama publish().
    #
    # cache_redis: decode_responses=True. La cache delle credenziali è
    # JSON testuale (json.dumps/json.loads) e zrange() deve restituire
    # str, non bytes, altrimenti le chiavi costruite con f-string nelle
    # funzioni get_cached_operational_credential ecc. si romperebbero.
    status_redis = redis_asyncio.from_url(
        os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
        decode_responses=False,
    )
    cache_redis = redis_asyncio.from_url(
        os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
        decode_responses=True,
    )
    status_list_manager = StatusListManager(
        redis_client=status_redis, authority_id=_IDENTITY_AUTHORITY_ID
    )

    identity_manager = IdentityManager(
        governing_authority_did=_GOVERNING_AUTHORITY_DID,
        signing_key=authority_key,
        signing_verification_method_id=_AUTHORITY_VM_ID,
        status_list_manager=status_list_manager,
        status_list_publish_url=_STATUS_LIST_PUBLISH_URL,
        audit_hook=_noop_audit_hook,
    )

    sentinel = Sentinel(trusted_issuer_keys={_AUTHORITY_VM_ID: authority_pub})

    _deps_singleton = _Deps(
        keystore, did_store, status_list_manager, identity_manager, sentinel,
        cache_redis, pool, _CREDENTIAL_LOG_TABLE, trust_registry_store,
        trust_registry_client,
    )
    return _deps_singleton


# ══════════════════════════════════════════════════════════════════════════════
# IDENTITY TOOLS
# ══════════════════════════════════════════════════════════════════════════════

async def bootstrap_agent_identity(agent_id: str) -> dict:
    """
    Genera (o recupera, se già esiste) l'identità DID di un agente
    della piattaforma e ne pubblica il DID Document.

    Idempotente: richiamarla su un agent_id già bootstrappato
    restituisce lo stesso DID, created=False — non genera mai una
    seconda chiave per lo stesso agente. Vale anche per un agente che
    diventerà Grantor: il bootstrap è identico a qualunque altro agente.

    Args:
        agent_id: identificativo dell'agente sulla piattaforma
            (es. "agent-087"), lo stesso usato oggi per KeyRecord.client_id.

    Returns:
        dict con 'did' (str) e 'created' (bool), oppure {'error': '...'}.
    """
    d = await _deps()
    try:
        _, public_key, created = bootstrap_signing_key(agent_id, d.keystore)
        document = build_did_document(
            host=_GOVERNING_AUTHORITY_HOST,
            agent_path_segments=["agents", agent_id],
            governing_authority_did=_GOVERNING_AUTHORITY_DID,
            signing_public_key=public_key,
        )
        await d.did_store.put(agent_id, document)
        return {"did": document.id, "created": created}
    except Exception as e:
        logger.error("bootstrap_agent_identity error: %s", e)
        return {"error": str(e)}


async def issue_identity_credential_for_agent(
    agent_id: str,
    roles: list[str] | None = None,
    principal_ref: str | None = None,
) -> dict:
    """
    Emette una Identity Credential per un agente GIÀ bootstrappato
    (bootstrap_agent_identity deve essere stata chiamata prima).

    Per rendere un agente un Grantor: passare roles=["grantor"] qui —
    è l'unico modo in cui questo tool "sa" di un Grantor, come dato
    dell'identità, mai come azione di delega (questo modulo non chiama
    mai DelegationManager).

    Args:
        agent_id: lo stesso agent_id passato a bootstrap_agent_identity.
        roles: ruoli dichiarati (es. ["grantor"]).
        principal_ref: riferimento opaco al principal umano/organizzazione
            responsabile di questo agente (es. "org:sparkle:bda").

    Returns:
        La credenziale firmata come dict JSON-compatibile, oppure
        {'error': '...'} se l'agente non è ancora bootstrappato.
    """
    d = await _deps()
    try:
        document = await d.did_store.get(agent_id)
        if document is None:
            return {"error": f"agent '{agent_id}' non ha un DID — chiamare prima bootstrap_agent_identity"}

        credential = await d.identity_manager.issue_identity_credential(
            agent_did=document.id,
            client_id=agent_id,
            roles=roles or [],
            principal_ref=principal_ref,
        )
        payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)

        # Log di audit — mai consultato da validate_agent_identity_credential,
        # serve solo a rispondere "a chi avevamo emesso l'indice N" quando
        # la credenziale stessa non è più recuperabile (persa, mai cachata,
        # cache scaduta). Se questo INSERT fallisce, l'emissione è già
        # avvenuta e la credenziale è comunque valida — logghiamo l'errore
        # ma non la invalidiamo per un problema del solo log.
        try:
            async with d.pg_pool.acquire() as conn:
                await conn.execute(
                    f"""INSERT INTO {d.credential_log_table}
                        (credential_id, agent_id, status_list_index, issued_at,
                         valid_until, roles, principal_ref)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                        ON CONFLICT (credential_id) DO NOTHING""",
                    credential.id, agent_id, credential.credential_status.status_list_index,
                    credential.valid_from, credential.valid_until,
                    json.dumps(roles or []), principal_ref,
                )
        except Exception as e:
            logger.error("credential log INSERT fallito (credenziale comunque valida): %s", e)

        return payload
    except Exception as e:
        logger.error("issue_identity_credential_for_agent error: %s", e)
        return {"error": str(e)}


async def list_agents_with_identity() -> dict:
    """
    Elenca tutti gli agenti della piattaforma che hanno già un DID
    pubblicato — l'inventario delle identità, non delle credenziali
    (quelle non restano cachate qui: una Identity Credential viaggia
    con chi la riceve, stesso principio "Cosa Viaggia, Cosa È
    Pubblicato" del resto del sistema).

    Returns:
        dict con 'agent_ids' (lista, ordine alfabetico) e 'count'.
    """
    d = await _deps()
    try:
        agent_ids = await d.did_store.list_agent_ids()
        return {"agent_ids": agent_ids, "count": len(agent_ids)}
    except Exception as e:
        logger.error("list_agents_with_identity error: %s", e)
        return {"error": str(e)}


async def get_agent_did_document(agent_id: str) -> dict:
    """
    Recupera il DID Document pubblicato per un agente della piattaforma.

    Args:
        agent_id: l'identificativo dell'agente.

    Returns:
        dict con 'found' (bool) e 'document' se trovato.
    """
    d = await _deps()
    try:
        document = await d.did_store.get(agent_id)
        if document is None:
            return {"found": False}
        return {"found": True, "document": document.model_dump(mode="json", by_alias=True)}
    except Exception as e:
        logger.error("get_agent_did_document error: %s", e)
        return {"error": str(e)}


async def validate_agent_identity_credential(credential: dict) -> dict:
    """
    Verifica DAVVERO una Identity Credential — firma, finestra di
    validità, stato di revoca — non un semplice lookup. Stesso
    controllo (Sentinel.verify()) che farebbe una piattaforma federata
    in produzione. Utile in demo per mostrare sia il caso valido sia,
    dopo una revoca, il fail-closed.

    Args:
        credential: il dict esatto restituito da
            issue_identity_credential_for_agent.

    Returns:
        dict con 'valid' (bool) e 'reason' se non valida.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
    except Exception as e:
        return {"valid": False, "reason": f"credenziale malformata: {e}"}

    try:
        now = dt.datetime.now(dt.timezone.utc)
        is_revoked = await d.status_list_manager.is_revoked(
            parsed.credential_status.status_list_index
        )
        await d.sentinel.verify(parsed, status_bitstring_fetch_time=now, is_revoked=is_revoked)
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_agent_identity_credential error: %s", e)
        return {"error": str(e)}


async def revoke_agent_identity_credential(credential: dict) -> dict:
    """
    Revoca una Identity Credential precedentemente emessa.

    Invalida anche la cache dell'agente (stls:identity:credential:{agent_id})
    se contiene esattamente questa credenziale (stesso id) — senza
    questo passo, get_cached_identity_credential continuerebbe a
    restituire un JSON dall'aspetto perfettamente valido anche dopo la
    revoca: la credenziale in sé non cambia mai, la revoca vive solo
    nella status list esterna, quindi la cache va invalidata
    esplicitamente, non si accorge da sola di nulla.

    Non cancella alla cieca: se nel frattempo l'agente ha una
    credenziale DIVERSA (più recente) in cache, quella resta — solo
    quella che corrisponde esattamente alla revoca viene tolta.

    Args:
        credential: il dict esatto restituito da
            issue_identity_credential_for_agent — non un id, l'intero
            oggetto (serve per risalire a quale voce della status list
            puntare).

    Returns:
        dict con 'revoked': True, 'credential_id' e 'cache_invalidated'
        (bool — indica se la cache conteneva proprio questa credenziale),
        oppure {'error': '...'}.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
        await d.identity_manager.revoke(parsed)

        cache_invalidated = False
        agent_id = parsed.credential_subject.client_id
        cache_key = f"{_CRED_KEY_PREFIX}:{agent_id}"
        cached_raw = await d.cache_redis.get(cache_key)
        if cached_raw is not None:
            cached = json.loads(cached_raw)
            if cached.get("id") == parsed.id:
                await d.cache_redis.delete(cache_key)
                cache_invalidated = True

        # Stesso spirito del log INSERT in issue_identity_credential_for_agent:
        # se questo UPDATE fallisce, la revoca vera (sulla status list) è
        # comunque già avvenuta — la credenziale è già invalida, un log
        # incompleto non deve far sembrare fallita una revoca riuscita.
        try:
            async with d.pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {d.credential_log_table} SET revoked_at = $2 WHERE credential_id = $1",
                    parsed.id, dt.datetime.now(dt.timezone.utc),
                )
        except Exception as e:
            logger.error("credential log UPDATE fallito (revoca comunque avvenuta): %s", e)

        return {
            "revoked": True,
            "credential_id": parsed.id,
            "cache_invalidated": cache_invalidated,
        }
    except Exception as e:
        logger.error("revoke_agent_identity_credential error: %s", e)
        return {"error": str(e)}


async def describe_platform_identity_state() -> dict:
    """
    Panoramica rapida dello stato delle identità sulla piattaforma —
    pensata per demo ("mostrami la situazione attuale"), non per un
    audit completo.

    Returns:
        dict con 'governing_authority_did', 'agents_with_identity'
        (conteggio) e 'agent_ids'.
    """
    d = await _deps()
    try:
        agent_ids = await d.did_store.list_agent_ids()
        return {
            "governing_authority_did": _GOVERNING_AUTHORITY_DID,
            "agents_with_identity": len(agent_ids),
            "agent_ids": agent_ids,
        }
    except Exception as e:
        logger.error("describe_platform_identity_state error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL CACHE
# ══════════════════════════════════════════════════════════════════════════════
#
# Non è il "registro centrale delle credenziali emesse" che questo sistema
# esplicitamente non tiene (vedi il resto del modulo) — è lo storage
# dell'agente per la PROPRIA credenziale, stesso perimetro di fiducia del
# keystore che già tiene la sua chiave privata. Chiunque possa già leggere
# la chiave privata di un agente è già dentro il perimetro giusto per
# leggere anche la sua credenziale, che finirà comunque presentata
# pubblicamente ad ogni chiamata — è meno sensibile della chiave, non di più.
#
# TTL = validUntil della credenziale, non un valore fisso: se l'agente
# cerca la propria credenziale e non la trova, il segnale è "è scaduta o
# non l'ho mai chiesta" — nessuna logica di scadenza da scrivere a mano,
# si pulisce da sola (stesso principio self-cleaning già usato altrove
# nel sistema per gli score di scadenza).

def _ttl_seconds_until(valid_until_iso: str) -> int:
    valid_until = dt.datetime.fromisoformat(valid_until_iso.replace("Z", "+00:00"))
    remaining = (valid_until - dt.datetime.now(dt.timezone.utc)).total_seconds()
    return max(int(remaining), 0)


async def cache_own_identity_credential(agent_id: str, credential: dict) -> dict:
    """
    Salva la Identity Credential di un agente nel suo storage privato —
    slot singolo (un'identità alla volta ha senso, non due). TTL uguale
    a validUntil della credenziale: scade da sola quando la credenziale
    non è più valida, nessuna pulizia manuale necessaria.

    Args:
        agent_id: l'agente a cui appartiene la credenziale.
        credential: il dict esatto restituito da
            issue_identity_credential_for_agent.

    Returns:
        dict con 'cached': True e 'ttl_seconds', oppure {'error': '...'}
        se la credenziale è già scaduta o malformata.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
    except Exception as e:
        return {"error": f"credenziale malformata: {e}"}

    ttl = _ttl_seconds_until(parsed.valid_until.isoformat()) if parsed.valid_until else None
    if ttl is not None and ttl <= 0:
        return {"error": "la credenziale è già scaduta, non la salvo"}

    try:
        key = f"{_CRED_KEY_PREFIX}:{agent_id}"
        payload = json.dumps(credential)
        if ttl is not None:
            await d.cache_redis.set(key, payload, ex=ttl)
        else:
            await d.cache_redis.set(key, payload)
        return {"cached": True, "ttl_seconds": ttl}
    except Exception as e:
        logger.error("cache_own_identity_credential error: %s", e)
        return {"error": str(e)}


async def get_cached_identity_credential(agent_id: str) -> dict:
    """
    Recupera la Identity Credential salvata di un agente, se presente e
    non scaduta.

    Args:
        agent_id: l'agente di cui recuperare la credenziale.

    Returns:
        dict con 'found': True e 'credential', oppure {'found': False}
        se non presente o scaduta (stessa cosa, dal punto di vista di
        chi chiama: il TTL le rende indistinguibili).
    """
    d = await _deps()
    try:
        raw = await d.cache_redis.get(f"{_CRED_KEY_PREFIX}:{agent_id}")
        if raw is None:
            return {"found": False}
        return {"found": True, "credential": json.loads(raw)}
    except Exception as e:
        logger.error("get_cached_identity_credential error: %s", e)
        return {"error": str(e)}


async def cache_operational_credential(agent_id: str, credential: dict) -> dict:
    """
    Salva una Operational Credential ricevuta in delega nello storage
    privato dell'agente — collezione, non slot singolo: un agente può
    detenere più deleghe contemporaneamente, da Grantor diversi, per
    scope diversi. TTL = validUntil, stessa logica self-cleaning della
    Identity Credential; l'indice (sorted set, score = validUntil epoch)
    si ripulisce da sé ad ogni list_cached_operational_credentials.

    Args:
        agent_id: l'agente che detiene la delega (il Requestor).
        credential: il dict di una OperationalCredential (es. da uno
            script/agente Grantor esterno a questo modulo — questo tool
            non ne emette mai, solo le mette in cache per chi le riceve).

    Returns:
        dict con 'cached': True, 'credential_id', 'ttl_seconds', oppure
        {'error': '...'} se già scaduta o malformata.
    """
    d = await _deps()
    try:
        parsed = OperationalCredential.model_validate(credential)
    except Exception as e:
        return {"error": f"credenziale malformata: {e}"}

    if parsed.valid_until is None:
        return {"error": "Operational Credential senza validUntil — non cacheabile in sicurezza"}

    ttl = _ttl_seconds_until(parsed.valid_until.isoformat())
    if ttl <= 0:
        return {"error": "la credenziale è già scaduta, non la salvo"}

    try:
        key = f"{_OPCRED_KEY_PREFIX}:{agent_id}:{parsed.id}"
        index_key = f"{_OPCRED_INDEX_PREFIX}:{agent_id}"
        expires_at_epoch = parsed.valid_until.timestamp()

        await d.cache_redis.set(key, json.dumps(credential), ex=ttl)
        await d.cache_redis.zadd(index_key, {parsed.id: expires_at_epoch})
        return {"cached": True, "credential_id": parsed.id, "ttl_seconds": ttl}
    except Exception as e:
        logger.error("cache_operational_credential error: %s", e)
        return {"error": str(e)}


async def list_cached_operational_credentials(agent_id: str) -> dict:
    """
    Elenca le Operational Credential attualmente valide (non scadute)
    in cache per un agente. Ripulisce prima le entry scadute
    dall'indice — stesso pattern self-cleaning già usato altrove nel
    sistema (ZREMRANGEBYSCORE sugli score di scadenza).

    Args:
        agent_id: l'agente di cui elencare le deleghe cachate.

    Returns:
        dict con 'credential_ids' (lista) e 'count'.
    """
    d = await _deps()
    try:
        index_key = f"{_OPCRED_INDEX_PREFIX}:{agent_id}"
        now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()

        await d.cache_redis.zremrangebyscore(index_key, "-inf", now_epoch)
        credential_ids = await d.cache_redis.zrange(index_key, 0, -1)
        return {"credential_ids": list(credential_ids), "count": len(credential_ids)}
    except Exception as e:
        logger.error("list_cached_operational_credentials error: %s", e)
        return {"error": str(e)}


async def get_cached_operational_credential(agent_id: str, credential_id: str) -> dict:
    """
    Recupera una specifica Operational Credential cachata per un agente.

    Args:
        agent_id: l'agente che detiene la delega.
        credential_id: l'id della credenziale (da
            list_cached_operational_credentials).

    Returns:
        dict con 'found': True e 'credential', oppure {'found': False}
        se non presente o scaduta.
    """
    d = await _deps()
    try:
        raw = await d.cache_redis.get(f"{_OPCRED_KEY_PREFIX}:{agent_id}:{credential_id}")
        if raw is None:
            return {"found": False}
        return {"found": True, "credential": json.loads(raw)}
    except Exception as e:
        logger.error("get_cached_operational_credential error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# STATUS LIST — verifica pubblicazione via HTTP
# ══════════════════════════════════════════════════════════════════════════════

async def check_published_status_list() -> dict:
    """
    Verifica la status list COSÌ COME PUBBLICATA dal bridge via HTTP —
    non legge Redis direttamente, controlla l'intera pipeline (Redis ->
    publish() -> cache Postgres -> HTTP). Utile per accorgersi se il
    refresher del bridge si è mai inceppato (stesso bug capitato in
    produzione: decode_responses=True rompeva silenziosamente
    publish(), e la status list pubblicata restava vuota per sempre —
    questo tool l'avrebbe reso visibile in un turno di conversazione
    invece che con una sessione di debug su redis-cli).

    Mostra SOLO gli indici revocati (numeri interi), mai a quale agente
    corrispondono — non perché quella mappa non esista (vedi
    find_agent_by_status_list_index, che la consulta in un log di
    audit separato), ma perché la bitstring pubblica in sé dice solo
    "l'indice N è revocato", mai "agent-002 è revocato": chi legge
    questo endpoint da fuori la piattaforma non ha comunque accesso al
    log di audit. Per risalire all'agente da qui dentro, vedi
    find_agent_by_status_list_index.

    Returns:
        dict con 'valid_from', 'total_bits', 'revoked_indices' (lista
        di interi) e 'revoked_count', oppure {'error': '...'} se il
        bridge non risponde o pubblica ancora un placeholder vuoto.
    """
    url = _STATUS_LIST_PUBLISH_URL
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.error("check_published_status_list error: %s", e)
        return {"error": f"impossibile leggere la status list pubblicata su {url}: {e}"}

    encoded_list = payload.get("encodedList", "")
    if not encoded_list:
        return {"error": "il bridge ha risposto ma encodedList è vuoto/assente — refresher mai andato a buon fine?"}

    try:
        padded = encoded_list + "=" * (-len(encoded_list) % 4)
        raw = base64.urlsafe_b64decode(padded)
        decompressed = gzip.decompress(raw)
    except Exception as e:
        return {"error": f"impossibile decodificare encodedList (base64/gzip): {e}"}

    revoked_indices = [
        byte_idx * 8 + bit_idx
        for byte_idx, byte in enumerate(decompressed)
        for bit_idx in range(8)
        if (byte >> (7 - bit_idx)) & 1
    ]

    return {
        "valid_from": payload.get("validFrom"),
        "total_bits": len(decompressed) * 8,
        "revoked_indices": revoked_indices,
        "revoked_count": len(revoked_indices),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL LOG — audit, mai usato per la verifica
# ══════════════════════════════════════════════════════════════════════════════

async def find_agent_by_status_list_index(status_list_index: int) -> dict:
    """
    Risponde a "a chi avevamo emesso questo indice della status list" —
    consulta SOLO il log di audit (sf_identity_credential_log di
    default), MAI la status list stessa o Sentinel.verify(). Questo
    tool non dice se la credenziale è ancora valida (per quello vedi
    validate_agent_identity_credential o check_published_status_list)
    — dice solo a chi apparteneva quell'indice, storicamente.

    Args:
        status_list_index: l'indice trovato, ad esempio, tra i
            revoked_indices di check_published_status_list.

    Returns:
        dict con 'found': bool e, se trovato, 'agent_id',
        'credential_id', 'issued_at', 'revoked_at' (None se non
        revocata), 'roles', 'principal_ref'.
    """
    d = await _deps()
    try:
        async with d.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT credential_id, agent_id, issued_at, valid_until,
                           roles, principal_ref, revoked_at
                    FROM {d.credential_log_table}
                    WHERE status_list_index = $1
                    ORDER BY issued_at DESC""",
                status_list_index,
            )
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "credential_id": row["credential_id"],
            "agent_id": row["agent_id"],
            "issued_at": row["issued_at"].isoformat() if row["issued_at"] else None,
            "valid_until": row["valid_until"].isoformat() if row["valid_until"] else None,
            "roles": json.loads(row["roles"]) if row["roles"] else [],
            "principal_ref": row["principal_ref"],
            "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        }
    except Exception as e:
        logger.error("find_agent_by_status_list_index error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TRUST ANCHOR REGISTRY — Sparkle come anchor operator (Opzione A)
# ══════════════════════════════════════════════════════════════════════════════
#
# CRUD di base sulle Authority riconosciute dalla federazione. Lo store
# del package supporta anche storia delle chiavi e revoca puntuale
# (revoke_key, non solo revoke_authority) — qui esposti solo onboard/
# list/revoke, lo scope "molto semplice" concordato. La verifica di
# credenziali emesse da un'Authority esterna resta fuori: richiederebbe
# anche contattare il bridge di quella piattaforma per la sua status
# list, un pezzo a sé che non vogliamo costruire a metà.

async def onboard_trust_anchor(
    authority_id: str,
    did: str,
    verification_method_id: str,
    public_key_multibase: str,
) -> dict:
    """
    Registra un'Authority esterna come riconosciuta dalla federazione.

    I quattro parametri si copiano direttamente dal DID Document
    pubblicato dall'altra piattaforma (GET /.well-known/did.json o
    equivalente): did = il suo id, verification_method_id e
    public_key_multibase = dalla voce corrispondente in
    verificationMethod. Nessuna chiamata di rete qui — l'onboarding è
    un'azione amministrativa deliberata, non automatica (Opzione A:
    centralizzato, Sparkle decide chi fa parte della federazione).

    Args:
        authority_id: id breve, leggibile, per uso amministrativo (es.
            "operatore-b") — non crittografico, solo bookkeeping.
        did: il DID della Governing Authority esterna (es.
            "did:web:b.example.com").
        verification_method_id: l'id della chiave (es.
            "did:web:b.example.com#key-1").
        public_key_multibase: la chiave pubblica, copiata esattamente
            dal loro DID Document (formato multibase, es. "z6Mk...").

    Returns:
        dict con 'onboarded': True e 'authority_id', oppure {'error': '...'}.
    """
    d = await _deps()
    try:
        entry = TrustAnchorEntry(
            authority_id=authority_id,
            did=did,
            verification_method_id=verification_method_id,
            public_key_multibase=public_key_multibase,
            onboarded_at=dt.datetime.now(dt.timezone.utc),
        )
        await d.trust_registry_store.put(entry)
        return {"onboarded": True, "authority_id": authority_id, "did": did}
    except Exception as e:
        logger.error("onboard_trust_anchor error: %s", e)
        return {"error": str(e)}


async def list_trust_anchors() -> dict:
    """
    Elenca le Authority attualmente riconosciute dalla federazione
    (generazione di chiave corrente, non revocata).

    Returns:
        dict con 'anchors' (lista di {authority_id, did,
        verification_method_id, onboarded_at}) e 'count'.
    """
    d = await _deps()
    try:
        entries = await d.trust_registry_store.list_active()
        anchors = [
            {
                "authority_id": e.authority_id,
                "did": e.did,
                "verification_method_id": e.verification_method_id,
                "onboarded_at": e.onboarded_at.isoformat() if e.onboarded_at else None,
            }
            for e in entries
        ]
        return {"anchors": anchors, "count": len(anchors)}
    except Exception as e:
        logger.error("list_trust_anchors error: %s", e)
        return {"error": str(e)}


async def revoke_trust_anchor(authority_id: str) -> dict:
    """
    Offboarding di un'Authority dalla federazione — chiude OGNI
    generazione di chiave che ha mai avuto (storia inclusa), non solo
    quella corrente. Nessuna riga viene cancellata, resta audit.

    Args:
        authority_id: l'id passato a suo tempo a onboard_trust_anchor.

    Returns:
        dict con 'revoked': True e 'authority_id', oppure {'error': '...'}.
    """
    d = await _deps()
    try:
        await d.trust_registry_store.revoke_authority(
            authority_id, dt.datetime.now(dt.timezone.utc)
        )
        return {"revoked": True, "authority_id": authority_id}
    except Exception as e:
        logger.error("revoke_trust_anchor error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# VERIFICA CREDENZIALI ESTERNE — usa il Trust Anchor Registry per davvero
# ══════════════════════════════════════════════════════════════════════════════

_MULTICODEC_ED25519_PUB_PREFIX = b"\xed\x01"  # stesso multicodec usato da stls-identity per publicKeyMultibase


def _decode_multibase_ed25519_public_key(multibase_str: str) -> Ed25519PublicKey:
    """
    Ricostruisce una Ed25519PublicKey da un publicKeyMultibase — locale
    a questo file, non importata da stls_identity: la funzione
    equivalente del package non è esportata dal suo __init__.py nella
    versione installata (ImportError riscontrato in produzione), quindi
    la teniamo qui invece di dipendere da un dettaglio interno instabile.
    Stessa logica già verificata contro una chiave reale (quella di
    Operatore B, onboardata nel Trust Anchor Registry).
    """
    if not multibase_str.startswith("z"):
        raise ValueError(f"atteso prefisso multibase 'z' (base58btc), trovato: {multibase_str[:1]!r}")
    raw = b58decode(multibase_str[1:])
    prefix, key_bytes = raw[:2], raw[2:]
    if prefix != _MULTICODEC_ED25519_PUB_PREFIX:
        raise ValueError(
            f"multicodec inatteso {prefix!r}, atteso {_MULTICODEC_ED25519_PUB_PREFIX!r} (ed25519-pub)"
        )
    return Ed25519PublicKey.from_public_bytes(key_bytes)


async def _is_bit_revoked_at_url(status_list_url: str, index: int) -> bool:
    """Fetch + decodifica (base64url + gzip) di una status list pubblicata,
    controllo del singolo bit. Terza volta che questa logica serve
    (dopo check_published_status_list e validate_external_identity_credential)
    — estratta qui invece di duplicata una terza volta."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(status_list_url)
    resp.raise_for_status()
    payload = resp.json()
    encoded_list = payload.get("encodedList", "")
    if not encoded_list:
        raise ValueError(f"status list vuota presso {status_list_url}")
    padded = encoded_list + "=" * (-len(encoded_list) % 4)
    decompressed = gzip.decompress(base64.urlsafe_b64decode(padded))
    byte_idx, bit_idx = index // 8, index % 8
    return bool(len(decompressed) > byte_idx and (decompressed[byte_idx] >> (7 - bit_idx)) & 1)


async def validate_external_identity_credential(credential: dict) -> dict:
    """
    Verifica una Identity Credential emessa da un'Authority ESTERNA
    (non da questa piattaforma) — a differenza di
    validate_agent_identity_credential, che conosce solo la propria
    chiave e la propria status list, questo:
      1. risolve la chiave dell'issuer tramite il Trust Anchor Registry
         (deve essere già stata onboardata con onboard_trust_anchor,
         altrimenti fallisce con "unknown or untrusted verification
         method" — correttamente, non è un bug)
      2. controlla la revoca presso IL BRIDGE DELL'ISSUER, non il
         nostro — l'URL non va configurato a mano, è dentro la
         credenziale stessa (credentialStatus.statusListCredential)

    Args:
        credential: il dict di una NetworkAuthorityCredential emessa da
            un'altra piattaforma (es. l'Identity Manager di Operatore B).

    Returns:
        dict con 'valid': bool, e 'reason' se non valida.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
    except Exception as e:
        return {"valid": False, "reason": f"credenziale malformata: {e}"}

    try:
        await d.trust_registry_client.refresh()
        trusted_keys = d.trust_registry_client.as_trusted_keys()
    except Exception as e:
        logger.error("validate_external_identity_credential: refresh registro fallito: %s", e)
        return {"error": f"impossibile leggere il trust anchor registry: {e}"}

    status_list_url = parsed.credential_status.status_list_credential
    try:
        is_revoked = await _is_bit_revoked_at_url(
            status_list_url, parsed.credential_status.status_list_index
        )
    except Exception as e:
        logger.error("validate_external_identity_credential: status list fallita: %s", e)
        return {"valid": False, "reason": f"impossibile verificare la revoca presso {status_list_url}: {e}"}

    external_sentinel = Sentinel(trusted_issuer_keys=trusted_keys)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        await external_sentinel.verify(parsed, status_bitstring_fetch_time=now, is_revoked=is_revoked)
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_external_identity_credential error: %s", e)
        return {"error": str(e)}


async def validate_operational_credential(
    operational_credential: dict,
    grantor_identity_credential: dict,
) -> dict:
    """
    Verifica una catena di delega — Operational Credential + Identity
    Credential del Grantor — quando il Grantor è un agente DI QUESTA
    PIATTAFORMA. Tre controlli in uno, via
    Sentinel.verify_delegation_chain(): (a) l'Identity Credential del
    Grantor supera da sola firma/finestra/revoca, (b) l'issuer della
    Operational Credential coincide col soggetto di quella Identity
    Credential, (c) la Operational Credential stessa supera
    firma/finestra/revoca — sulla propria status list, non quella
    dell'Identity Credential (indici diversi, entry diverse).

    Per un Grantor esterno vedi validate_external_operational_credential.

    Args:
        operational_credential: il dict della delega da verificare.
        grantor_identity_credential: il dict dell'Identity Credential
            del Grantor che l'ha emessa.

    Returns:
        dict con 'valid': bool, e 'reason' se non valida.
    """
    d = await _deps()
    try:
        op = OperationalCredential.model_validate(operational_credential)
        grantor_id_cred = NetworkAuthorityCredential.model_validate(grantor_identity_credential)
    except Exception as e:
        return {"valid": False, "reason": f"credenziale malformata: {e}"}

    now = dt.datetime.now(dt.timezone.utc)
    try:
        grantor_revoked = await d.status_list_manager.is_revoked(
            grantor_id_cred.credential_status.status_list_index
        )
        op_revoked = await d.status_list_manager.is_revoked(
            op.credential_status.status_list_index
        )
        await d.sentinel.verify_delegation_chain(
            op,
            grantor_identity_credential=grantor_id_cred,
            grantor_identity_fetch_time=now, grantor_identity_is_revoked=grantor_revoked,
            operational_fetch_time=now, operational_is_revoked=op_revoked,
        )
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_operational_credential error: %s", e)
        return {"error": str(e)}


async def validate_external_operational_credential(
    operational_credential: dict,
    grantor_identity_credential: dict,
    grantor_did_document: dict,
) -> dict:
    """
    Verifica una catena di delega quando il Grantor è un agente di
    UN'ALTRA PIATTAFORMA. Rispetto al caso interno serve un dato in
    più: il Trust Anchor Registry contiene solo chiavi di Governing
    Authority, mai le chiavi dei singoli agenti — quindi la chiave del
    Grantor stesso (quella che ha firmato la Operational Credential)
    non è risolvibile da lì. Va fornita esplicitamente come il suo DID
    Document, copiato dal bridge di quella piattaforma (es.
    GET {host}/agents/{id}/did.json) — stesso principio già usato per
    onboard_trust_anchor: nessuna risoluzione automatica, un dato
    copiato deliberatamente.

    La revoca di ENTRAMBE le credenziali si controlla presso i
    rispettivi bridge (URL dentro ciascuna credenziale,
    credentialStatus.statusListCredential) — mai il nostro.

    Args:
        operational_credential: il dict della delega da verificare.
        grantor_identity_credential: il dict dell'Identity Credential
            del Grantor, emessa dalla SUA Governing Authority (deve
            essere già stata onboardata con onboard_trust_anchor).
        grantor_did_document: il DID Document del Grantor stesso,
            copiato dal bridge della sua piattaforma.

    Returns:
        dict con 'valid': bool, e 'reason' se non valida.
    """
    d = await _deps()
    try:
        op = OperationalCredential.model_validate(operational_credential)
        grantor_id_cred = NetworkAuthorityCredential.model_validate(grantor_identity_credential)
    except Exception as e:
        return {"valid": False, "reason": f"credenziale malformata: {e}"}

    try:
        vm = grantor_did_document["verificationMethod"][0]
        grantor_pub = _decode_multibase_ed25519_public_key(vm["publicKeyMultibase"])
        grantor_vm_id = vm["id"]
    except Exception as e:
        return {"valid": False, "reason": f"grantor_did_document malformato: {e}"}

    try:
        await d.trust_registry_client.refresh()
        trusted_keys = dict(d.trust_registry_client.as_trusted_keys())
    except Exception as e:
        logger.error("validate_external_operational_credential: refresh registro fallito: %s", e)
        return {"error": f"impossibile leggere il trust anchor registry: {e}"}
    trusted_keys[grantor_vm_id] = grantor_pub  # la chiave del Grantor, mai nel registro
    external_sentinel = Sentinel(trusted_issuer_keys=trusted_keys)

    now = dt.datetime.now(dt.timezone.utc)
    try:
        grantor_revoked = await _is_bit_revoked_at_url(
            grantor_id_cred.credential_status.status_list_credential,
            grantor_id_cred.credential_status.status_list_index,
        )
        op_revoked = await _is_bit_revoked_at_url(
            op.credential_status.status_list_credential,
            op.credential_status.status_list_index,
        )
    except Exception as e:
        return {"valid": False, "reason": f"impossibile verificare la revoca: {e}"}

    try:
        await external_sentinel.verify_delegation_chain(
            op,
            grantor_identity_credential=grantor_id_cred,
            grantor_identity_fetch_time=now, grantor_identity_is_revoked=grantor_revoked,
            operational_fetch_time=now, operational_is_revoked=op_revoked,
        )
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_external_operational_credential error: %s", e)
        return {"error": str(e)}