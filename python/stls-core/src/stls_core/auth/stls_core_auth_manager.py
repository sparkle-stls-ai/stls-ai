"""
stls_core_auth_manager.py
------------------------
Orchestratore principale del sistema di autenticazione API key STLS.

Componenti:
    default_audit_hook()    — audit hook standard basato su logging (factory)
    ApiKeyManager           — valida le chiavi, applica scope/endpoint check,
                              scrive l'audit log.
    ApiKeyManagerGroup      — gestisce il lifecycle di un insieme di manager
                              con una sola chiamata (start/stop/health/refresh).
    BackgroundRefresher     — asyncio task che ricarica periodicamente tutte
                              le chiavi dal loader → Redis.

Flusso di validazione per ogni richiesta:
    1. Estrai raw_key dall'header (X-API-Key) o query param (?api_key=).
    2. hash(raw_key) → key_id  (plaintext scartato subito).
    3. RedisKeyCache.is_revoked(key_id) → 401 immediato.
    4. RedisKeyCache.get(key_id) → cache hit → check expiry/scope/endpoint.
    5. Cache miss → loader.load_one(key_id) → RedisKeyCache.set() → check.
    6. Scrivi AuditEntry (logging strutturato + hook opzionale).
    7. Restituisce ValidationResult.

Integrazione con il middleware esistente (stls_core_integration.py):

    # Inizializzazione (una volta sola, all'avvio dell'app)
    manager = ApiKeyManager.from_loader(loader)
    await manager.start()

    # In SecureA2AHTTPMiddleware.__call__():
    result = await manager.validate_request(request)
    if not result.ok:
        return _build_401(result)

    # Shutdown
    await manager.stop()

    # Oppure con ApiKeyManagerGroup:
    group = ApiKeyManagerGroup().add("static", sm).add("file", fm)
    async with group:
        ...
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .stls_core_auth_loaders import ApiKeyLoader, create_loader
from .stls_core_auth_models import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    AuthResult,
    AuditEntry,
    KeyRecord,
    ValidationResult,
    hash_key,
)
import socket
from .stls_core_auth_store import RedisKeyCache

logger = logging.getLogger(__name__)

# Tipo per l'hook di audit esterno (es. invio a SIEM, Pub/Sub, ecc.)
AuditHook = Callable[[AuditEntry], Awaitable[None]]


# ---------------------------------------------------------------------------
# Audit hook di default
# ---------------------------------------------------------------------------

def default_audit_hook(
    logger_instance: logging.Logger | None = None,
) -> AuditHook:
    """
    Factory che restituisce un audit_hook standard basato su logging.

    Logga ogni tentativo di autenticazione con livello INFO (OK) o WARNING
    (denied). Adatto per la maggior parte dei deployment; in produzione
    sostituire con un hook che invia a SIEM/Pub-Sub/Cloud Logging.

    Args:
        logger_instance: Logger da usare. Default: ``stls_core.audit``.

    Returns:
        Coroutine callable compatibile con il tipo ``AuditHook``.

    Esempio::

        manager = ApiKeyManager.from_loader(loader, audit_hook=default_audit_hook())

        # Con logger custom:
        manager = ApiKeyManager.from_loader(
            loader,
            audit_hook=default_audit_hook(logging.getLogger("myapp.security")),
        )
    """
    _log = logger_instance or logging.getLogger("stls_core.audit")

    async def _hook(entry: AuditEntry) -> None:
        level = logging.INFO if entry.result.value == "ok" else logging.WARNING
        _log.log(
            level,
            "[AUDIT] result=%-12s client=%-25s endpoint=%-40s addr=%s req_id=%s",
            entry.result.value,
            entry.client_id or "—",
            entry.endpoint,
            entry.remote_addr or "—",
            entry.request_id or "—",
        )

    return _hook


def _namespace_for_loader(loader: ApiKeyLoader) -> str:
    """
    Deriva il namespace Redis dal tipo di loader.

    Garantisce che loader distinti sullo stesso Redis (es. StaticLoader +
    FileLoader + PostgresLoader nello stesso server) usino chiavi separate
    e non si sovrascrivano durante bulk_reload.

    Mapping:
        StaticLoader    → "stls:static"
        FileLoader      → "stls:file"
        PostgresLoader  → "stls:pg"
        altro           → "stls"   (compatibilità con versioni precedenti)

    Se si usano più istanze dello stesso tipo (raro), passare ``namespace``
    esplicitamente a ``from_loader()``.
    """
    loader_type = type(loader).__name__
    _map = {
        "StaticLoader":   "stls:static",
        "FileLoader":     "stls:file",
        "PostgresLoader": "stls:pg",
    }
    ns = _map.get(loader_type, "stls")
    logger.debug("_namespace_for_loader: %s → %s", loader_type, ns)
    return ns


# ---------------------------------------------------------------------------
# ApiKeyManager
# ---------------------------------------------------------------------------

class ApiKeyManager:
    """
    Orchestratore della validazione API key.

    Args:
        loader:           Implementazione ApiKeyLoader (Static/File/Postgres).
        cache:            Istanza RedisKeyCache già configurata.
        refresh_interval: Secondi tra un reload e l'altro del BackgroundRefresher.
                          Default: env AUTH_REFRESH_INTERVAL, poi 300s.
        audit_hook:       Callback async opzionale chiamato dopo ogni validazione.
                          Riceve l'AuditEntry completo. Utile per SIEM / Pub/Sub.
                          Se None, viene usato default_audit_hook() automaticamente.
        enable_cache:     Se False, bypassa Redis e interroga sempre il loader.
                          Utile per ambienti senza Redis o per test.
    """

    def __init__(
        self,
        loader: ApiKeyLoader,
        cache: RedisKeyCache,
        refresh_interval: int | None = None,
        audit_hook: AuditHook | None = None,
        enable_cache: bool = True,
    ) -> None:
        self._loader = loader
        self._cache = cache
        self._refresh_interval = refresh_interval or int(
            os.environ.get("AUTH_REFRESH_INTERVAL", DEFAULT_REFRESH_INTERVAL_SECONDS)
        )
        # Se non viene passato un hook esplicito usa il default basato su logging.
        # Questo garantisce che ogni tentativo sia sempre loggato senza obbligare
        # ogni server a definire la stessa boilerplate.
        self._audit_hook = audit_hook if audit_hook is not None else default_audit_hook()
        self._enable_cache = enable_cache
        self._refresher: BackgroundRefresher | None = None
        # Trattiene una reference ai task di audit fire-and-forget finché non
        # completano — senza, l'unica reference sarebbe quella interna al
        # loop, che asyncio non garantisce contro la garbage collection
        # prematura del task prima della sua schedulazione.
        self._audit_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_loader(
        cls,
        loader: ApiKeyLoader,
        *,
        redis_url: str | None = None,
        enable_cache: bool | None = None,
        audit_hook: AuditHook | None = None,
        namespace: str | None = None,
        **kwargs: Any,
    ) -> "ApiKeyManager":
        """
        Factory principale: crea un ApiKeyManager da un loader già costruito.

        Legge REDIS_URL e AUTH_KM_USE_REDIS dall'env se non specificati
        esplicitamente, eliminando la necessità di gestire queste variabili
        nel codice del server.

        Args:
            loader:       Istanza ApiKeyLoader (StaticLoader, FileLoader, PostgresLoader).
            redis_url:    URL Redis. Default: env REDIS_URL, poi redis://localhost:6379.
            enable_cache: Abilita cache Redis. Default: env AUTH_KM_USE_REDIS (true).
            audit_hook:   Hook di audit. Default: default_audit_hook().
            namespace:    Namespace Redis per isolare le chiavi di questo loader.
                          Default: derivato automaticamente dal tipo di loader
                          (es. StaticLoader → "stls:static", FileLoader → "stls:file",
                          PostgresLoader → "stls:pg").
                          Passare un valore esplicito se si usano più istanze dello
                          stesso tipo di loader sullo stesso Redis.
                          Per un singolo manager in produzione usare il default "stls".
            **kwargs:     Parametri aggiuntivi passati al costruttore.

        Returns:
            ApiKeyManager configurato e pronto per await .start().

        Esempio::

            static_manager = ApiKeyManager.from_loader(
                StaticLoader.from_plaintext({raw_key: {...}}),
            )
            file_manager = ApiKeyManager.from_loader(
                FileLoader(path="api_keys.yaml"),
            )
            pg_manager = ApiKeyManager.from_loader(
                PostgresLoader(dsn=os.getenv("AUTH_PG_DSN")),
            ) if os.getenv("AUTH_PG_DSN") else None

            # Namespace esplicito (es. più FileLoader diversi):
            keys_manager = ApiKeyManager.from_loader(loader, namespace="stls:keys-A")
        """
        _redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        if enable_cache is None:
            enable_cache = os.environ.get(
                "AUTH_KM_USE_REDIS", "true"
            ).lower() not in ("false", "0", "no")

        # Namespace auto-derivato dal tipo di loader se non specificato esplicitamente.
        # Garantisce che loader distinti sullo stesso Redis non si sovrascrivano a vicenda.
        _ns = namespace or _namespace_for_loader(loader)
        cache = RedisKeyCache(url=_redis_url, namespace=_ns)
        return cls(
            loader=loader,
            cache=cache,
            audit_hook=audit_hook,
            enable_cache=enable_cache,
            **kwargs,
        )

    @classmethod
    def from_env(
        cls,
        audit_hook: AuditHook | None = None,
        **kwargs: Any,
    ) -> "ApiKeyManager":
        """
        Crea un ApiKeyManager leggendo tutta la configurazione dall'ambiente.

        Env vars rilevanti:
            AUTH_LOADER_BACKEND   static | file | postgres  (default: static)
            AUTH_KEYS_FILE        path al file YAML/JSON    (per FileLoader)
            AUTH_PG_DSN           DSN PostgreSQL            (per PostgresLoader)
            AUTH_KEY_TTL          TTL Redis in secondi      (default: 3600)
            AUTH_REFRESH_INTERVAL secondi tra refresh       (default: 300)
            REDIS_URL             URL Redis                  (default: redis://localhost:6379)
            AUTH_KM_USE_REDIS     abilita cache Redis        (default: true)
        """
        loader = create_loader()
        return cls.from_loader(loader, audit_hook=audit_hook, **kwargs)

    @classmethod
    def from_static(
        cls,
        keys: dict[str, dict[str, Any]],
        use_redis: bool = True,
        **kwargs: Any,
    ) -> "ApiKeyManager":
        """
        Factory rapida per test/dev con chiavi in-memory.

        Args:
            keys:      dict {raw_key: {client_id, scopes, endpoints, ...}}
            use_redis: Se False, usa enable_cache=False (nessun Redis necessario).

        Esempio::

            manager = ApiKeyManager.from_static({
                "my-dev-key": {"client_id": "dev", "scopes": ["secure-a2a"]},
            }, use_redis=False)
        """
        from .stls_core_auth_loaders import StaticLoader
        loader = StaticLoader.from_plaintext(keys)
        return cls.from_loader(loader, enable_cache=use_redis, **kwargs)

    @classmethod
    def from_auth_config(
        cls,
        auth_config: Any,
        use_redis: bool = True,
        **kwargs: Any,
    ) -> "ApiKeyManager":
        """
        Factory di compatibilità con l'AuthConfig attuale di STLS.

        Migra le chiavi esistenti (api_keys + bearer_tokens) nel nuovo
        sistema senza modifiche alla configurazione dell'utente.

        Esempio in stls_core_integration.py::

            if auth_config:
                manager = ApiKeyManager.from_auth_config(auth_config)
                await manager.start()
        """
        from .stls_core_auth_loaders import StaticLoader
        loader = StaticLoader.from_auth_config(auth_config)
        return cls.from_loader(loader, enable_cache=use_redis, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Inizializza Redis, esegue il primo bulk_reload sincrono,
        avvia il BackgroundRefresher.

        Da chiamare una sola volta all'avvio dell'applicazione
        (es. in FastAPI lifespan o startup event).
        """
        if self._enable_cache:
            await self._cache.initialize()
            # primo caricamento sincrono prima di aprire il traffico
            await self._initial_load()

        distributed = os.environ.get("AUTH_REFRESH_DISTRIBUTED", "true").lower() \
            not in ("false", "0", "no")
        # Passa il namespace al refresher: ogni refresher usa il proprio lock Redis
        # indipendente, evitando che static/file/postgres si blocchino a vicenda.
        _ns = getattr(self._cache, "_namespace", "stls")
        self._refresher = BackgroundRefresher(
            loader=self._loader,
            cache=self._cache if self._enable_cache else None,
            interval=self._refresh_interval,
            distributed=distributed,
            namespace=_ns,
        )
        await self._refresher.start()
        logger.info(
            "ApiKeyManager avviato (loader=%s, cache=%s, refresh=%ds)",
            type(self._loader).__name__,
            self._enable_cache,
            self._refresh_interval,
        )

    async def stop(self) -> None:
        """
        Ferma il BackgroundRefresher e chiude le connessioni.

        Da chiamare nello shutdown dell'applicazione.
        """
        if self._refresher:
            await self._refresher.stop()
        if self._enable_cache:
            await self._cache.close()
        await self._loader.close()
        logger.info("ApiKeyManager fermato (loader=%s)", type(self._loader).__name__)

    async def __aenter__(self) -> "ApiKeyManager":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Validazione principale
    # ------------------------------------------------------------------

    async def validate(
        self,
        raw_key: str,
        endpoint: str,
        remote_addr: str = "",
        request_id: str = "",
    ) -> ValidationResult:
        """
        Valida una API key per un endpoint specifico.

        Args:
            raw_key:     Valore grezzo della chiave (da header/query param).
                         Viene hashato immediatamente; il plaintext non viene loggato.
            endpoint:    Path dell'endpoint richiesto (es. "/secure-a2a").
            remote_addr: IP del client, per l'audit log.
            request_id:  Trace ID della request, per l'audit log.

        Returns:
            ValidationResult con result, record (se OK), e audit entry.
        """
        ts = datetime.now(timezone.utc)

        # --- 1. hash immediato — plaintext scartato ---
        key_id = hash_key(raw_key)

        # --- 2. revocation check (O(1), prima di qualsiasi altra op) ---
        if self._enable_cache and await self._cache.is_revoked(key_id):
            return self._build_result(
                AuthResult.REVOKED, None, key_id, None, endpoint,
                remote_addr, request_id, ts,
            )

        # --- 3. lookup record ---
        record = await self._lookup(key_id)

        if record is None:
            return self._build_result(
                AuthResult.INVALID, None, key_id, None, endpoint,
                remote_addr, request_id, ts,
            )

        # --- 4. expiry check ---
        if record.is_expired():
            return self._build_result(
                AuthResult.EXPIRED, None, key_id, record.client_id, endpoint,
                remote_addr, request_id, ts,
            )

        # --- 5. endpoint scope check ---
        if not record.allows_endpoint(endpoint):
            return self._build_result(
                AuthResult.SCOPE_DENIED, None, key_id, record.client_id, endpoint,
                remote_addr, request_id, ts,
            )

        # --- 6. OK ---
        return self._build_result(
            AuthResult.OK, record, key_id, record.client_id, endpoint,
            remote_addr, request_id, ts,
        )

    async def validate_request(self, request: Any) -> ValidationResult:
        """
        Overload di comodità che estrae la chiave da una Starlette/FastAPI Request.

        Cerca la chiave in ordine:
            1. Header X-API-Key
            2. Query param ?api_key=

        Se nessuna chiave è presente restituisce ValidationResult con
        result=INVALID e record=None (trattato come 401 dal middleware).
        """
        raw_key = (
            request.headers.get("x-api-key")
            or request.query_params.get("api_key")
            or ""
        )
        endpoint = request.url.path
        remote_addr = self._extract_remote_addr(request)
        request_id = request.headers.get("x-request-id", "")

        if not raw_key:
            ts = datetime.now(timezone.utc)
            audit = AuditEntry(
                timestamp=ts,
                key_id=None,
                client_id=None,
                endpoint=endpoint,
                result=AuthResult.INVALID,
                remote_addr=remote_addr,
                request_id=request_id,
            )
            await self._emit_audit(audit)
            return ValidationResult(result=AuthResult.INVALID, record=None, audit=audit)

        return await self.validate(raw_key, endpoint, remote_addr, request_id)

    # ------------------------------------------------------------------
    # Gestione manuale delle chiavi (admin API)
    # ------------------------------------------------------------------

    async def revoke(self, key_id: str) -> None:
        """Revoca una chiave per key_id (SHA-256). Effetto immediato su Redis."""
        if self._enable_cache:
            await self._cache.revoke(key_id)
        logger.info("ApiKeyManager: chiave %s... revocata", key_id[:12])

    async def add_key(self, record: KeyRecord) -> None:
        """Aggiunge o aggiorna un KeyRecord in cache (senza toccare il loader)."""
        if self._enable_cache:
            await self._cache.set(record)

    async def health(self) -> dict[str, Any]:
        """
        Restituisce lo stato di salute del sistema di autenticazione.

        Usabile da un endpoint /health o /readiness dell'applicazione.
        Indica "ok" solo se Redis è raggiungibile (quando abilitato) e il
        refresher è in esecuzione.
        """
        redis_ok = await self._cache.ping() if self._enable_cache else None
        cache_stats = await self._cache.stats() if self._enable_cache else {}
        redis_status = "ok" if redis_ok else ("error" if redis_ok is False else "disabled")
        refresher_running = self._refresher.running if self._refresher else False
        # Degraded se Redis è in errore (ma non se semplicemente disabilitato)
        overall = "ok" if (redis_status in ("ok", "disabled") and refresher_running) else "degraded"
        return {
            "status": overall,
            "auth_manager": "ok",
            "redis": redis_status,
            "loader": type(self._loader).__name__,
            "refresher_running": refresher_running,
            **cache_stats,
        }

    # ------------------------------------------------------------------
    # Helpers interni
    # ------------------------------------------------------------------

    async def _initial_load(self) -> None:
        """Carica tutti i record dal loader → Redis prima di aprire il traffico."""
        try:
            records = await self._loader.load_all()
            if records:
                await self._cache.bulk_reload(records)
                logger.info(
                    "ApiKeyManager: caricati %d record dal loader all'avvio", len(records)
                )
            else:
                logger.warning(
                    "ApiKeyManager: nessun record caricato all'avvio "
                    "(loader=%s)", type(self._loader).__name__
                )
        except Exception as exc:
            logger.error("ApiKeyManager._initial_load() fallito: %s", exc)

    async def _lookup(self, key_id: str) -> KeyRecord | None:
        """
        Cerca il record: prima in Redis (se abilitato), poi nel loader.
        Su cache miss popola Redis per le prossime richieste.
        """
        if self._enable_cache:
            record = await self._cache.get(key_id)
            if record is not None:
                return record

        # cache miss (o cache disabilitata) → interroga il loader
        record = await self._loader.load_one(key_id)
        if record is not None and self._enable_cache:
            await self._cache.set(record)

        return record

    def _build_result(
        self,
        result: AuthResult,
        record: KeyRecord | None,
        key_id: str,
        client_id: str | None,
        endpoint: str,
        remote_addr: str,
        request_id: str,
        ts: datetime,
    ) -> ValidationResult:
        """Costruisce ValidationResult, scrive il log e chiama l'audit hook."""
        audit = AuditEntry(
            timestamp=ts,
            key_id=key_id,
            client_id=client_id,
            endpoint=endpoint,
            result=result,
            remote_addr=remote_addr,
            request_id=request_id,
        )

        # log strutturato (livello INFO per OK, WARNING per deny)
        if result == AuthResult.OK:
            logger.info(
                "auth OK client=%s endpoint=%s key=%s...",
                client_id, endpoint, key_id[:12],
            )
        else:
            logger.warning(
                "auth DENIED result=%s client=%s endpoint=%s key=%s...",
                result.value, client_id, endpoint, key_id[:12],
            )

        # fire-and-forget dell'audit hook (non blocca la risposta), con
        # reference trattenuta in self._audit_tasks finché non completa.
        if self._audit_hook:
            task = asyncio.ensure_future(self._emit_audit(audit))
            self._audit_tasks.add(task)
            task.add_done_callback(self._audit_tasks.discard)

        return ValidationResult(result=result, record=record, audit=audit)

    async def _emit_audit(self, audit: AuditEntry) -> None:
        """Chiama l'audit hook con gestione eccezioni isolata."""
        if self._audit_hook:
            try:
                await self._audit_hook(audit)
            except Exception as exc:
                logger.error("ApiKeyManager: audit hook fallito: %s", exc)

    @staticmethod
    def _extract_remote_addr(request: Any) -> str:
        """Estrae l'IP reale del client da X-Forwarded-For o client.host."""
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        client = getattr(request, "client", None)
        return getattr(client, "host", "") if client else ""


# ---------------------------------------------------------------------------
# ApiKeyManagerGroup
# ---------------------------------------------------------------------------

class ApiKeyManagerGroup:
    """
    Gestisce il lifecycle di un insieme di ApiKeyManager.

    Consente di avviare, fermare, fare health check e forzare refresh
    su tutti i manager registrati con una sola chiamata.

    Manager ``None`` vengono ignorati silenziosamente — questo permette
    di passare ``pg_manager`` anche quando Postgres non è configurato
    (ritorna None) senza if aggiuntivi nel server.

    Uso come async context manager (pattern raccomandato nel lifespan FastAPI)::

        group = (
            ApiKeyManagerGroup()
            .add("static", static_manager)
            .add("file",   file_manager)
            .add("postgres", pg_manager)   # può essere None → ignorato
        )

        @asynccontextmanager
        async def lifespan(app):
            async with group:          # start() all'ingresso, stop() all'uscita
                yield

    Uso manuale::

        await group.start()
        try:
            ...
        finally:
            await group.stop()
    """

    def __init__(self) -> None:
        self._managers: dict[str, ApiKeyManager] = {}

    # ------------------------------------------------------------------
    # Registrazione
    # ------------------------------------------------------------------

    def add(
        self,
        name: str,
        manager: "ApiKeyManager | None",
    ) -> "ApiKeyManagerGroup":
        """
        Aggiunge un manager al gruppo.

        Args:
            name:    Identificativo leggibile usato in log e health report.
            manager: Istanza ApiKeyManager oppure None (viene ignorata).

        Returns:
            self — permette chaining fluente.
        """
        if manager is not None:
            self._managers[name] = manager
        else:
            logger.debug(
                "ApiKeyManagerGroup.add('%s'): manager None, ignorato", name
            )
        return self

    def get(self, name: str) -> "ApiKeyManager | None":
        """Restituisce il manager con il nome dato, o None se non presente."""
        return self._managers.get(name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Avvia tutti i manager registrati in ordine di inserimento."""
        if not self._managers:
            logger.warning("ApiKeyManagerGroup.start(): nessun manager registrato")
            return
        logger.info(
            "ApiKeyManagerGroup: avvio %d manager (%s)…",
            len(self._managers),
            ", ".join(self._managers),
        )
        for name, mgr in self._managers.items():
            try:
                await mgr.start()
                logger.info("ApiKeyManagerGroup: '%s' avviato", name)
            except Exception as exc:
                logger.error(
                    "ApiKeyManagerGroup: errore avvio '%s': %s", name, exc
                )
                raise

    async def stop(self) -> None:
        """Ferma tutti i manager in ordine inverso di inserimento."""
        for name, mgr in reversed(list(self._managers.items())):
            try:
                await mgr.stop()
                logger.info("ApiKeyManagerGroup: '%s' fermato", name)
            except Exception as exc:
                logger.warning(
                    "ApiKeyManagerGroup: errore stop '%s': %s", name, exc
                )

    async def __aenter__(self) -> "ApiKeyManagerGroup":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Health & diagnostica
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """
        Stato di salute aggregato di tutti i manager.

        Returns:
            Dict con:
                ``status``         → "ok" | "degraded" (aggregato)
                ``{nome_manager}`` → health_dict per ciascun manager

        Un singolo manager in stato "degraded" porta l'intero gruppo a "degraded".
        """
        results: dict[str, Any] = {}
        for name, mgr in self._managers.items():
            try:
                results[name] = await mgr.health()
            except Exception as exc:
                results[name] = {"status": "error", "auth_manager": "error", "error": str(exc)}
        # Stato aggregato: ok solo se TUTTI i manager sono ok
        all_ok = all(
            r.get("status") == "ok"
            for r in results.values()
        )
        return {"status": "ok" if all_ok else "degraded", **results}

    async def refresh_all(self) -> dict[str, int]:
        """
        Forza un reload immediato delle chiavi da tutti i loader.

        Utile da un endpoint admin ``POST /auth/refresh`` dopo una
        rotazione di chiavi senza riavviare il server.

        Returns:
            Dict ``{nome_manager: n_record_caricati}``.
        """
        results: dict[str, int] = {}
        for name, mgr in self._managers.items():
            if mgr._refresher:
                try:
                    results[name] = await mgr._refresher.trigger()
                except Exception as exc:
                    logger.error(
                        "ApiKeyManagerGroup.refresh_all('%s'): %s", name, exc
                    )
                    results[name] = 0
        return results

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._managers)

    def __contains__(self, name: str) -> bool:
        return name in self._managers

    def __repr__(self) -> str:
        names = list(self._managers)
        return f"ApiKeyManagerGroup(managers={names})"


# ---------------------------------------------------------------------------
# BackgroundRefresher
# ---------------------------------------------------------------------------

class BackgroundRefresher:
    """
    Asyncio task che ricarica periodicamente tutte le chiavi
    dal loader → Redis.

    Gestisce rotazioni di chiave, revoche e aggiornamenti di scope
    senza riavvii del servizio.

    Il ciclo è best-effort: un'eccezione nel reload viene loggata
    e il refresher riprende al prossimo intervallo.

    Il lock Redis è per-namespace: più manager sullo stesso Redis
    (es. static + file + postgres) competono solo con le proprie
    repliche su altri nodi, non tra di loro.
    """

    def __init__(
        self,
        loader: ApiKeyLoader,
        cache: RedisKeyCache | None,
        interval: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
        distributed: bool = True,
        namespace: str = "stls",
    ) -> None:
        self._loader      = loader
        self._cache       = cache
        self._interval    = interval
        self._distributed = distributed
        self._namespace   = namespace
        # Lock key per-namespace: ogni loader ha il proprio lock indipendente.
        # Es.: stls:refresh:lock:stls:static  /  stls:refresh:lock:stls:pg
        # Static, file e postgres non si bloccano a vicenda.
        self._lock_key = f"stls:refresh:lock:{namespace}"
        # Identificativo univoco del nodo per il lock
        self._node_id  = f"{socket.gethostname()}:{os.getpid()}"
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Avvia il loop di refresh in background."""
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(), name=f"stls-auth-key-refresher-{self._namespace}"
        )
        logger.info(
            "BackgroundRefresher avviato (ns=%s, interval=%ds, cache=%s, distributed=%s, node=%s)",
            self._namespace,
            self._interval,
            "enabled" if self._cache else "disabled",
            self._distributed,
            self._node_id,
        )

    async def stop(self) -> None:
        """Ferma il loop di refresh e attende la terminazione."""
        if self._stop_event:
            self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("BackgroundRefresher fermato")

    async def trigger(self) -> int:
        """
        Forza un reload immediato bypassando il lock distribuito.
        Utile per: /auth/refresh admin endpoint, test, webhook di rotazione chiave.

        Returns:
            Numero di record caricati.
        """
        return await self._do_refresh(force=True)

    # ------------------------------------------------------------------
    # Loop interno
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Loop principale: aspetta l'intervallo, esegue il refresh, ripete."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                # stop_event settato: uscita pulita
                break
            except asyncio.TimeoutError:
                # intervallo scaduto normalmente → refresh
                pass

            await self._do_refresh()

    async def _do_refresh(self, force: bool = False) -> int:
        """
        Esegue il caricamento e l'aggiornamento della cache.

        In modalità distribuita (distributed=True, default) usa un Redis lock
        per garantire che un solo nodo alla volta esegua il reload da Postgres/File.
        Gli altri nodi vedono il lock, saltano il reload e leggono dalla cache
        già aggiornata dal nodo leader.

        Args:
            force: Se True bypassa il lock (usato da trigger() per admin reload).

        Returns:
            Numero di record caricati (0 se skippato o in caso di errore).
        """
        if self._distributed and self._cache is not None and not force:
            acquired = await self._try_acquire_lock()
            if not acquired:
                logger.debug(
                    "BackgroundRefresher [%s]: lock detenuto da altro nodo — skip",
                    self._node_id,
                )
                return 0
        else:
            acquired = False  # nessun lock da rilasciare

        try:
            records = await self._loader.load_all()
            if self._cache is not None:
                count = await self._cache.bulk_reload(records)
            else:
                count = len(records)
            logger.debug(
                "BackgroundRefresher [%s]: refresh completato, %d record",
                self._node_id, count,
            )
            return count
        except Exception as exc:
            logger.error(
                "BackgroundRefresher [%s]: refresh fallito: %s",
                self._node_id, exc,
            )
            return 0
        finally:
            if acquired:
                await self._release_lock()

    async def _try_acquire_lock(self) -> bool:
        """
        Tenta di acquisire il leader election lock in Redis.

        SET stls:refresh:lock:{namespace} <node_id> NX EX <interval+30>
        NX = solo se non esiste (atomico, nessuna race condition).
        TTL = intervallo + 30s di buffer per gestire crash del leader.

        Returns:
            True se il lock è stato acquisito, False se già detenuto.
        """
        try:
            result = await self._cache._client.set(
                self._lock_key,
                self._node_id,
                nx=True,
                ex=self._interval + 30,
            )
            return result is not None  # SET NX restituisce None se non acquisito
        except Exception as exc:
            logger.warning(
                "BackgroundRefresher: errore acquisizione lock: %s — procedo senza lock",
                exc,
            )
            return True  # fail-open: se Redis non risponde, refresha comunque

    async def _release_lock(self) -> None:
        """
        Rilascia il lock solo se è ancora detenuto da questo nodo.
        Usa uno script Lua per garantire atomicità (check + delete).
        """
        lua_script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        try:
            await self._cache._client.eval(
                lua_script, 1, self._lock_key, self._node_id
            )
        except Exception as exc:
            logger.warning("BackgroundRefresher: errore rilascio lock: %s", exc)