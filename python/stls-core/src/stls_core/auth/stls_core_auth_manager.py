"""
stls_core_auth_manager.py
------------------------
Main orchestrator of the STLS API key authentication system.

Components:
    default_audit_hook()    — standard logging-based audit hook (factory)
    ApiKeyManager           — validates keys, applies scope/endpoint checks,
                              writes the audit log.
    ApiKeyManagerGroup      — manages the lifecycle of a set of managers
                              with a single call (start/stop/health/refresh).
    BackgroundRefresher     — asyncio task that periodically reloads all
                              keys from the loader → Redis.

Validation flow for every request:
    1. Extract raw_key from the header (X-API-Key) or query param (?api_key=).
    2. hash(raw_key) → key_id  (plaintext discarded immediately).
    3. RedisKeyCache.is_revoked(key_id) → immediate 401.
    4. RedisKeyCache.get(key_id) → cache hit → check expiry/scope/endpoint.
    5. Cache miss → loader.load_one(key_id) → RedisKeyCache.set() → check.
    6. Write the AuditEntry (structured logging + optional hook).
    7. Return the ValidationResult.

Integration with the existing middleware (stls_core_integration.py):

    # Initialization (once, at app startup)
    manager = ApiKeyManager.from_loader(loader)
    await manager.start()

    # In SecureA2AHTTPMiddleware.__call__():
    result = await manager.validate_request(request)
    if not result.ok:
        return _build_401(result)

    # Shutdown
    await manager.stop()

    # Or with ApiKeyManagerGroup:
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

# Type for the external audit hook (e.g. sending to SIEM, Pub/Sub, etc.)
AuditHook = Callable[[AuditEntry], Awaitable[None]]


# ---------------------------------------------------------------------------
# Default audit hook
# ---------------------------------------------------------------------------

def default_audit_hook(
    logger_instance: logging.Logger | None = None,
) -> AuditHook:
    """
    Factory returning a standard, logging-based audit_hook.

    Logs every authentication attempt at INFO (OK) or WARNING (denied)
    level. Suited for most deployments; in production replace it with
    a hook that sends to SIEM/Pub-Sub/Cloud Logging.

    Args:
        logger_instance: Logger to use. Default: ``stls_core.audit``.

    Returns:
        An async callable compatible with the ``AuditHook`` type.

    Example::

        manager = ApiKeyManager.from_loader(loader, audit_hook=default_audit_hook())

        # With a custom logger:
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
    Derives the Redis namespace from the loader's type.

    Guarantees that distinct loaders on the same Redis (e.g.
    StaticLoader + FileLoader + PostgresLoader on the same server) use
    separate keys and don't overwrite each other during bulk_reload.

    Mapping:
        StaticLoader    → "stls:static"
        FileLoader      → "stls:file"
        PostgresLoader  → "stls:pg"
        other           → "stls"   (compatible with earlier versions)

    If multiple instances of the same type are used (rare), pass
    ``namespace`` explicitly to ``from_loader()``.
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
    Orchestrator of API key validation.

    Args:
        loader:           ApiKeyLoader implementation (Static/File/Postgres).
        cache:            An already-configured RedisKeyCache instance.
        refresh_interval: Seconds between BackgroundRefresher reload cycles.
                          Default: env AUTH_REFRESH_INTERVAL, then 300s.
        audit_hook:       Optional async callback invoked after every validation.
                          Receives the full AuditEntry. Useful for SIEM / Pub/Sub.
                          If None, default_audit_hook() is used automatically.
        enable_cache:     If False, bypasses Redis and always queries the loader.
                          Useful for environments without Redis or for tests.
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
        # If no explicit hook is passed, falls back to the logging-based default.
        # This guarantees that every attempt is always logged without
        # forcing every server to define the same boilerplate.
        self._audit_hook = audit_hook if audit_hook is not None else default_audit_hook()
        self._enable_cache = enable_cache
        self._refresher: BackgroundRefresher | None = None
        # Keeps a reference to fire-and-forget audit tasks until they
        # complete — without it, the only reference would be the
        # loop's internal one, which asyncio does not guarantee against
        # premature garbage collection of the task before it's scheduled.
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
        Main factory: creates an ApiKeyManager from an already-built loader.

        Reads REDIS_URL and AUTH_KM_USE_REDIS from the environment when
        not explicitly specified, removing the need to handle these
        variables in the server's code.

        Args:
            loader:       ApiKeyLoader instance (StaticLoader, FileLoader, PostgresLoader).
            redis_url:    Redis URL. Default: env REDIS_URL, then redis://localhost:6379.
            enable_cache: Enables the Redis cache. Default: env AUTH_KM_USE_REDIS (true).
            audit_hook:   Audit hook. Default: default_audit_hook().
            namespace:    Redis namespace to isolate this loader's keys.
                          Default: automatically derived from the loader's
                          type (e.g. StaticLoader → "stls:static", FileLoader → "stls:file",
                          PostgresLoader → "stls:pg").
                          Pass an explicit value if multiple instances of the
                          same loader type run on the same Redis.
                          For a single production manager, use the default "stls".
            **kwargs:     Extra parameters passed to the constructor.

        Returns:
            A configured ApiKeyManager, ready for await .start().

        Example::

            static_manager = ApiKeyManager.from_loader(
                StaticLoader.from_plaintext({raw_key: {...}}),
            )
            file_manager = ApiKeyManager.from_loader(
                FileLoader(path="api_keys.yaml"),
            )
            pg_manager = ApiKeyManager.from_loader(
                PostgresLoader(dsn=os.getenv("AUTH_PG_DSN")),
            ) if os.getenv("AUTH_PG_DSN") else None

            # Explicit namespace (e.g. several distinct FileLoaders):
            keys_manager = ApiKeyManager.from_loader(loader, namespace="stls:keys-A")
        """
        _redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        if enable_cache is None:
            enable_cache = os.environ.get(
                "AUTH_KM_USE_REDIS", "true"
            ).lower() not in ("false", "0", "no")

        # Namespace auto-derived from the loader's type if not explicitly specified.
        # Guarantees that distinct loaders on the same Redis never overwrite each other.
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
        Creates an ApiKeyManager reading the entire configuration from the environment.

        Relevant env vars:
            AUTH_LOADER_BACKEND   static | file | postgres  (default: static)
            AUTH_KEYS_FILE        path to the YAML/JSON file (for FileLoader)
            AUTH_PG_DSN           PostgreSQL DSN            (for PostgresLoader)
            AUTH_KEY_TTL          Redis TTL in seconds       (default: 3600)
            AUTH_REFRESH_INTERVAL seconds between refreshes  (default: 300)
            REDIS_URL             Redis URL                  (default: redis://localhost:6379)
            AUTH_KM_USE_REDIS     enables the Redis cache     (default: true)
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
        Quick factory for tests/dev with in-memory keys.

        Args:
            keys:      dict {raw_key: {client_id, scopes, endpoints, ...}}
            use_redis: If False, uses enable_cache=False (no Redis needed).

        Example::

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
        Compatibility factory for STLS's current AuthConfig.

        Migrates existing keys (api_keys + bearer_tokens) into the new
        system with no changes to the user's configuration.

        Example in stls_core_integration.py::

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
        Initializes Redis, runs the first synchronous bulk_reload,
        starts the BackgroundRefresher.

        To be called once at application startup
        (e.g. in a FastAPI lifespan or startup event).
        """
        if self._enable_cache:
            await self._cache.initialize()
            # first synchronous load before opening up to traffic
            await self._initial_load()

        distributed = os.environ.get("AUTH_REFRESH_DISTRIBUTED", "true").lower() \
            not in ("false", "0", "no")
        # Passes the namespace to the refresher: each refresher uses its
        # own independent Redis lock, so that static/file/postgres don't
        # block each other.
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
        Stops the BackgroundRefresher and closes the connections.

        To be called at application shutdown.
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
    # Main validation
    # ------------------------------------------------------------------

    async def validate(
        self,
        raw_key: str,
        endpoint: str,
        remote_addr: str = "",
        request_id: str = "",
    ) -> ValidationResult:
        """
        Validates an API key for a specific endpoint.

        Args:
            raw_key:     Raw key value (from the header/query param).
                         Hashed immediately; the plaintext is never logged.
            endpoint:    Path of the requested endpoint (e.g. "/secure-a2a").
            remote_addr: Client IP, for the audit log.
            request_id:  Request trace ID, for the audit log.

        Returns:
            ValidationResult with the result, the record (if OK), and the audit entry.
        """
        ts = datetime.now(timezone.utc)

        # --- 1. hash immediately — plaintext discarded ---
        key_id = hash_key(raw_key)

        # --- 2. revocation check (O(1), before anything else) ---
        if self._enable_cache and await self._cache.is_revoked(key_id):
            return self._build_result(
                AuthResult.REVOKED, None, key_id, None, endpoint,
                remote_addr, request_id, ts,
            )

        # --- 3. record lookup ---
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
        Convenience overload that extracts the key from a Starlette/FastAPI Request.

        Looks for the key in order:
            1. X-API-Key header
            2. ?api_key= query param

        If no key is present, returns a ValidationResult with
        result=INVALID and record=None (treated as a 401 by the middleware).
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
    # Manual key management (admin API)
    # ------------------------------------------------------------------

    async def revoke(self, key_id: str) -> None:
        """Revokes a key by key_id (SHA-256). Immediate effect on Redis."""
        if self._enable_cache:
            await self._cache.revoke(key_id)
        logger.info("ApiKeyManager: chiave %s... revocata", key_id[:12])

    async def add_key(self, record: KeyRecord) -> None:
        """Adds or updates a KeyRecord in the cache (without touching the loader)."""
        if self._enable_cache:
            await self._cache.set(record)

    async def health(self) -> dict[str, Any]:
        """
        Returns the health status of the authentication system.

        Usable from a /health or /readiness application endpoint.
        Reports "ok" only if Redis is reachable (when enabled) and the
        refresher is running.
        """
        redis_ok = await self._cache.ping() if self._enable_cache else None
        cache_stats = await self._cache.stats() if self._enable_cache else {}
        redis_status = "ok" if redis_ok else ("error" if redis_ok is False else "disabled")
        refresher_running = self._refresher.running if self._refresher else False
        # Degraded if Redis is in error (but not if it's simply disabled)
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
    # Internal helpers
    # ------------------------------------------------------------------

    async def _initial_load(self) -> None:
        """Loads all records from the loader → Redis before opening up to traffic."""
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
        Looks up the record: first in Redis (if enabled), then in the loader.
        On a cache miss, populates Redis for subsequent requests.
        """
        if self._enable_cache:
            record = await self._cache.get(key_id)
            if record is not None:
                return record

        # cache miss (or cache disabled) → query the loader
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
        """Builds the ValidationResult, writes the log and calls the audit hook."""
        audit = AuditEntry(
            timestamp=ts,
            key_id=key_id,
            client_id=client_id,
            endpoint=endpoint,
            result=result,
            remote_addr=remote_addr,
            request_id=request_id,
        )

        # structured log (INFO level for OK, WARNING for deny)
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

        # fire-and-forget audit hook (doesn't block the response), with
        # a reference kept in self._audit_tasks until it completes.
        if self._audit_hook:
            task = asyncio.ensure_future(self._emit_audit(audit))
            self._audit_tasks.add(task)
            task.add_done_callback(self._audit_tasks.discard)

        return ValidationResult(result=result, record=record, audit=audit)

    async def _emit_audit(self, audit: AuditEntry) -> None:
        """Calls the audit hook with isolated exception handling."""
        if self._audit_hook:
            try:
                await self._audit_hook(audit)
            except Exception as exc:
                logger.error("ApiKeyManager: audit hook fallito: %s", exc)

    @staticmethod
    def _extract_remote_addr(request: Any) -> str:
        """Extracts the client's real IP from X-Forwarded-For or client.host."""
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
    Manages the lifecycle of a set of ApiKeyManagers.

    Allows starting, stopping, health-checking and forcing a refresh
    on all registered managers with a single call.

    ``None`` managers are silently ignored — this allows passing
    ``pg_manager`` even when Postgres isn't configured (returns None)
    with no extra ifs in the server code.

    Usage as an async context manager (the recommended pattern in a
    FastAPI lifespan)::

        group = (
            ApiKeyManagerGroup()
            .add("static", static_manager)
            .add("file",   file_manager)
            .add("postgres", pg_manager)   # can be None → ignored
        )

        @asynccontextmanager
        async def lifespan(app):
            async with group:          # start() on entry, stop() on exit
                yield

    Manual usage::

        await group.start()
        try:
            ...
        finally:
            await group.stop()
    """

    def __init__(self) -> None:
        self._managers: dict[str, ApiKeyManager] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add(
        self,
        name: str,
        manager: "ApiKeyManager | None",
    ) -> "ApiKeyManagerGroup":
        """
        Adds a manager to the group.

        Args:
            name:    Human-readable identifier used in logs and the health report.
            manager: ApiKeyManager instance, or None (ignored).

        Returns:
            self — allows fluent chaining.
        """
        if manager is not None:
            self._managers[name] = manager
        else:
            logger.debug(
                "ApiKeyManagerGroup.add('%s'): manager None, ignorato", name
            )
        return self

    def get(self, name: str) -> "ApiKeyManager | None":
        """Returns the manager with the given name, or None if not present."""
        return self._managers.get(name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Starts all registered managers in insertion order."""
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
        """Stops all managers in reverse insertion order."""
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
    # Health & diagnostics
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """
        Aggregated health status of all managers.

        Returns:
            A dict with:
                ``status``         → "ok" | "degraded" (aggregated)
                ``{manager_name}`` → that manager's health dict

        A single manager in "degraded" state brings the whole group to "degraded".
        """
        results: dict[str, Any] = {}
        for name, mgr in self._managers.items():
            try:
                results[name] = await mgr.health()
            except Exception as exc:
                results[name] = {"status": "error", "auth_manager": "error", "error": str(exc)}
        # Aggregated status: ok only if ALL managers are ok
        all_ok = all(
            r.get("status") == "ok"
            for r in results.values()
        )
        return {"status": "ok" if all_ok else "degraded", **results}

    async def refresh_all(self) -> dict[str, int]:
        """
        Forces an immediate reload of the keys from all loaders.

        Useful from an admin ``POST /auth/refresh`` endpoint after a
        key rotation, without restarting the server.

        Returns:
            Dict ``{manager_name: n_records_loaded}``.
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
    Asyncio task that periodically reloads all keys
    from the loader → Redis.

    Handles key rotations, revocations and scope updates
    with no service restarts.

    The cycle is best-effort: an exception during a reload is logged
    and the refresher resumes on the next interval.

    The Redis lock is per-namespace: multiple managers on the same
    Redis (e.g. static + file + postgres) only compete with their own
    replicas on other nodes, not with each other.
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
        # Per-namespace lock key: each loader has its own independent lock.
        # E.g.: stls:refresh:lock:stls:static  /  stls:refresh:lock:stls:pg
        # Static, file and postgres don't block each other.
        self._lock_key = f"stls:refresh:lock:{namespace}"
        # Unique node identifier for the lock
        self._node_id  = f"{socket.gethostname()}:{os.getpid()}"
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Starts the background refresh loop."""
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
        """Stops the refresh loop and waits for it to terminate."""
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
        Forces an immediate reload, bypassing the distributed lock.
        Useful for: the /auth/refresh admin endpoint, tests, a key-rotation webhook.

        Returns:
            Number of records loaded.
        """
        return await self._do_refresh(force=True)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main loop: waits for the interval, runs the refresh, repeats."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                # stop_event set: clean exit
                break
            except asyncio.TimeoutError:
                # interval elapsed normally → refresh
                pass

            await self._do_refresh()

    async def _do_refresh(self, force: bool = False) -> int:
        """
        Runs the load and the cache update.

        In distributed mode (distributed=True, default) uses a Redis
        lock to guarantee that only one node at a time reloads from
        Postgres/File. The other nodes see the lock, skip the reload
        and read from the cache already updated by the leader node.

        Args:
            force: If True, bypasses the lock (used by trigger() for admin reloads).

        Returns:
            Number of records loaded (0 if skipped or on error).
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
            acquired = False  # no lock to release

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
        Attempts to acquire the leader-election lock in Redis.

        SET stls:refresh:lock:{namespace} <node_id> NX EX <interval+30>
        NX = only if it doesn't already exist (atomic, no race condition).
        TTL = interval + a 30s buffer to handle a leader crash.

        Returns:
            True if the lock was acquired, False if already held.
        """
        try:
            result = await self._cache._client.set(
                self._lock_key,
                self._node_id,
                nx=True,
                ex=self._interval + 30,
            )
            return result is not None  # SET NX returns None if not acquired
        except Exception as exc:
            logger.warning(
                "BackgroundRefresher: errore acquisizione lock: %s — procedo senza lock",
                exc,
            )
            return True  # fail-open: if Redis doesn't respond, refresh anyway

    async def _release_lock(self) -> None:
        """
        Releases the lock only if it's still held by this node.
        Uses a Lua script to guarantee atomicity (check + delete).
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
