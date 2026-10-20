"""
tools_redis.py
--------------
Tools per sofia_redis_agent.
Naviga i key namespace di STLS-A2A su Redis:
  - stls:task:{id}        → task data (JSON)
  - stls:ctx:{context_id} → context data (JSON)
  - stls:index            → sorted set dei task IDs
  - stls:key:{sha256}     → API key record (JSON)
  - stls:revoked          → SET di key_id revocati

Env vars attese (con defaults):
  REDIS_URL    redis://localhost:6379
  REDIS_DB     0
"""

from __future__ import annotations

import json
import os
import logging
import re
import time
from typing import Optional

import redis as _redis

logger = logging.getLogger(__name__)

# ── Connessione ────────────────────────────────────────────────────────────────

def _client() -> _redis.Redis:
    """Restituisce un client Redis sync (non pooled, usa short-lived connections)."""
    return _redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379"),
        db=int(os.environ.get("REDIS_DB", 0)),
        decode_responses=True,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_json(raw: str | None) -> dict | list | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


# ══════════════════════════════════════════════════════════════════════════════
# TASK TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def list_tasks(
    limit: int = 50,
    offset: int = 0,
    newest_first: bool = True,
) -> dict:
    """
    Elenca i task IDs presenti nell'indice Redis (stls:index).

    Args:
        limit:        Numero massimo di task da restituire (default 50).
        offset:       Offset per la paginazione (default 0).
        newest_first: Se True, ordina dal più recente (score descrescente).

    Returns:
        dict con 'task_ids' (lista), 'total' (int nel sorted set), 'offset', 'limit'.
    """
    r = _client()
    try:
        total = r.zcard("stls:index")
        if newest_first:
            ids = r.zrevrange("stls:index", offset, offset + limit - 1, withscores=False)
        else:
            ids = r.zrange("stls:index", offset, offset + limit - 1, withscores=False)
        return {"task_ids": list(ids), "total": total, "offset": offset, "limit": limit}
    except Exception as e:
        logger.error("list_tasks error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_task(task_id: str) -> dict:
    """
    Recupera il payload completo di un task da Redis.

    Args:
        task_id: ID del task (es. "abc123").

    Returns:
        dict con il contenuto del task oppure {"error": "..."} se non trovato.
    """
    r = _client()
    try:
        raw = r.get(f"stls:task:{task_id}")
        if raw is None:
            return {"error": f"Task '{task_id}' non trovato"}
        data = _safe_json(raw)
        return {"task_id": task_id, "data": data}
    except Exception as e:
        logger.error("get_task error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_task_context(context_id: str) -> dict:
    """
    Recupera il contesto associato a un task (stls:ctx:{context_id}).

    Args:
        context_id: ID del context.

    Returns:
        dict con i dati del contesto.
    """
    r = _client()
    try:
        raw = r.get(f"stls:ctx:{context_id}")
        if raw is None:
            return {"error": f"Context '{context_id}' non trovato"}
        return {"context_id": context_id, "data": _safe_json(raw)}
    except Exception as e:
        logger.error("get_task_context error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def search_tasks_by_state(state: str, limit: int = 20) -> dict:
    """
    Cerca task filtrati per stato (es. 'working', 'completed', 'failed', 'submitted').
    Scansiona le chiavi stls:task:* e filtra per il campo 'status.state'.

    Args:
        state: Stato da cercare (es. 'working', 'completed', 'failed', 'submitted').
        limit: Numero massimo di risultati (default 20).

    Returns:
        dict con 'tasks' (lista di {task_id, state, context_id}) e 'count'.
    """
    r = _client()
    try:
        results = []
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match="stls:task:*", count=100)
            for key in keys:
                raw = r.get(key)
                data = _safe_json(raw)
                if isinstance(data, dict):
                    task_state = (data.get("status") or {}).get("state", "")
                    if task_state == state:
                        results.append({
                            "task_id": data.get("id", key.split(":", 2)[-1]),
                            "state": task_state,
                            "context_id": data.get("contextId"),
                            "created_at": (data.get("status") or {}).get("timestamp"),
                        })
                        if len(results) >= limit:
                            break
            if cursor == 0 or len(results) >= limit:
                break
        return {"tasks": results, "count": len(results), "state_filter": state}
    except Exception as e:
        logger.error("search_tasks_by_state error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_tasks_stats() -> dict:
    """
    Restituisce statistiche aggregate sui task: totale nell'indice,
    conteggio per stato, TTL medio.

    Returns:
        dict con 'total_in_index', 'counts_by_state', 'total_scanned'.
    """
    r = _client()
    try:
        total_in_index = r.zcard("stls:index")
        counts: dict[str, int] = {}
        total_scanned = 0
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match="stls:task:*", count=200)
            for key in keys:
                total_scanned += 1
                raw = r.get(key)
                data = _safe_json(raw)
                if isinstance(data, dict):
                    state = (data.get("status") or {}).get("state", "unknown")
                    counts[state] = counts.get(state, 0) + 1
            if cursor == 0:
                break
        return {
            "total_in_index": total_in_index,
            "total_scanned": total_scanned,
            "counts_by_state": counts,
        }
    except Exception as e:
        logger.error("get_tasks_stats error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


# ══════════════════════════════════════════════════════════════════════════════
# SESSION CACHE TOOLS  (stls:sc:* — RedisSessionCache)
# ══════════════════════════════════════════════════════════════════════════════
#
# Namespace: SESSION_CACHE_REDIS_NAMESPACE (default "stls:sc")
# Struttura di ogni entry: {"session_key": "<base64>", "kid": "<kid>", "created_at": <ts>}
# TTL: sliding (GETEX), default 3600s (SESSION_CACHE_TTL)
#
# Il campo 'session_key' NON viene mai restituito da questi tool per sicurezza.
# Il 'kid' consente di stimare il provider:
#   UUID format    → pqc   (ogni handshake PQC genera un UUID fresco)
#   base-session-* → base
#   altro          → arqit_py


def list_stls_sessions(
    limit: int = 50,
    offset: int = 0,
    provider_filter: Optional[str] = None,
) -> dict:
    """
    Elenca le sessioni STLS attive nella RedisSessionCache.

    Args:
        limit:           Numero massimo di sessioni da restituire (default 50).
        offset:          Offset per la paginazione (default 0).
        provider_filter: Filtra per provider stimato: 'pqc', 'arqit_py', 'base'.
                         None = tutte.

    Returns:
        dict con 'sessions' (lista), 'count', 'total_found', 'namespace'.
        Ogni sessione include: session_id, kid, provider_hint, created_at, ttl_seconds.
        Il campo session_key NON è incluso per sicurezza.
    """
    r = _client()
    try:
        pattern = f"{_SC_PREFIX}:*"
        all_sessions: list[dict] = []
        cursor = 0

        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=200)
            for k in keys:
                raw = r.get(k)
                data = _safe_json(raw) if raw else {}
                if not isinstance(data, dict):
                    continue

                kid = data.get("kid", "")
                hint = _provider_hint(kid)

                if provider_filter and hint != provider_filter:
                    continue

                session_id = k[len(_SC_PREFIX) + 1:]  # strip "stls:sc:"
                ttl = r.ttl(k)

                all_sessions.append({
                    "session_id":    session_id,
                    "kid":           kid,
                    "provider_hint": hint,
                    "created_at":    data.get("created_at"),
                    "ttl_seconds":   ttl,
                })
            if cursor == 0:
                break

        total_found = len(all_sessions)
        page = all_sessions[offset: offset + limit]
        return {
            "sessions":    page,
            "count":       len(page),
            "total_found": total_found,
            "namespace":   _SC_PREFIX,
            "provider_filter": provider_filter,
        }
    except Exception as e:
        logger.error("list_stls_sessions error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_stls_session(session_id: str) -> dict:
    """
    Recupera i metadati di una specifica sessione STLS.
    Il campo session_key NON è incluso per sicurezza.

    Args:
        session_id: ID della sessione (UUID o altro formato).

    Returns:
        dict con session_id, kid, provider_hint, created_at, ttl_seconds.
    """
    r = _client()
    try:
        key = f"{_SC_PREFIX}:{session_id}"
        raw = r.get(key)
        if raw is None:
            return {"error": f"Sessione '{session_id}' non trovata (scaduta o inesistente)"}

        data = _safe_json(raw)
        if not isinstance(data, dict):
            return {"error": "Dati sessione non validi", "raw": raw}

        kid = data.get("kid", "")
        ttl = r.ttl(key)
        return {
            "session_id":    session_id,
            "kid":           kid,
            "provider_hint": _provider_hint(kid),
            "created_at":    data.get("created_at"),
            "ttl_seconds":   ttl,
            "redis_key":     key,
        }
    except Exception as e:
        logger.error("get_stls_session error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_stls_sessions_stats() -> dict:
    """
    Statistiche aggregate sulle sessioni STLS attive in Redis.

    Returns:
        dict con totale, conteggio per provider_hint (pqc/arqit_py/base),
        TTL medio/min/max, sessione più vecchia e più recente.
    """
    r = _client()
    try:
        pattern = f"{_SC_PREFIX}:*"
        by_provider: dict[str, int] = {}
        ttls: list[int] = []
        oldest: float | None = None
        newest: float | None = None
        total = 0

        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=200)
            for k in keys:
                raw = r.get(k)
                data = _safe_json(raw) if raw else {}
                if not isinstance(data, dict):
                    continue

                total += 1
                hint = _provider_hint(data.get("kid", ""))
                by_provider[hint] = by_provider.get(hint, 0) + 1

                ttl = r.ttl(k)
                if ttl > 0:
                    ttls.append(ttl)

                created = data.get("created_at")
                if isinstance(created, (int, float)):
                    if oldest is None or created < oldest:
                        oldest = created
                    if newest is None or created > newest:
                        newest = created

            if cursor == 0:
                break

        result: dict = {
            "total_active_sessions": total,
            "by_provider":           by_provider,
            "namespace":             _SC_PREFIX,
        }
        if ttls:
            result["ttl_avg_seconds"] = round(sum(ttls) / len(ttls))
            result["ttl_min_seconds"] = min(ttls)
            result["ttl_max_seconds"] = max(ttls)
        if oldest:
            import datetime
            result["oldest_session_at"] = datetime.datetime.fromtimestamp(oldest).isoformat()
        if newest:
            result["newest_session_at"] = datetime.datetime.fromtimestamp(newest).isoformat()

        return result
    except Exception as e:
        logger.error("get_stls_sessions_stats error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def delete_stls_session(session_id: str) -> dict:
    """
    Elimina una sessione STLS dalla cache Redis, forzando il client a ripetere
    l'handshake. Usare per invalidare sessioni compromesse o scadute in anticipo.

    Args:
        session_id: ID della sessione da eliminare.

    Returns:
        dict con 'deleted' (bool) e 'session_id'.
    """
    r = _client()
    try:
        key = f"{_SC_PREFIX}:{session_id}"
        deleted = r.delete(key)
        return {
            "session_id": session_id,
            "deleted":    bool(deleted),
            "redis_key":  key,
        }
    except Exception as e:
        logger.error("delete_stls_session error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


# ══════════════════════════════════════════════════════════════════════════════
# API KEY TOOLS (Redis cache layer)
# ══════════════════════════════════════════════════════════════════════════════


# Prefissi per loader — stls:{loader}:key:{sha256}
_KEY_LOADER_PREFIXES = [
    "stls:file:key:",
    "stls:pg:key:",
    "stls:static:key:",
]

# Indici per loader
_KEY_INDEX_KEYS = [
    "stls:file:keys:index",
    "stls:pg:keys:index",
    "stls:static:keys:index",
    "stls:keys:index",          # eventuale indice globale
]

# Prefisso session cache — corrisponde a SESSION_CACHE_REDIS_NAMESPACE (default stls:sc)
_SC_PREFIX = os.environ.get("SESSION_CACHE_REDIS_NAMESPACE", "stls:sc")

# Regex per distinguere PQC (UUID kid) da arqit_py / base
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _provider_hint(kid: str) -> str:
    """
    Stima il provider dal formato del kid:
    - UUID → pqc (ogni handshake genera un UUID fresco)
    - 'base-session-key-identifier' → base
    - altro → arqit_py
    """
    if not kid:
        return "unknown"
    if _UUID_RE.match(kid):
        return "pqc"
    if kid == "base-session-key-identifier":
        return "base"
    return "arqit_py"


def list_cached_api_keys(limit: int = 50) -> dict:
    """
    Elenca le API key presenti nella cache Redis per tutti i loader
    (stls:file:key:*, stls:pg:key:*, stls:static:key:*).

    Args:
        limit: Numero massimo di chiavi da restituire (default 50).

    Returns:
        dict con 'keys' (lista di {redis_key, loader, key_id}), 'count',
        'by_loader' (conteggio per loader).
    """
    r = _client()
    try:
        found: list[dict] = []
        by_loader: dict[str, int] = {}

        for prefix in _KEY_LOADER_PREFIXES:
            loader = prefix.split(":")[1]   # "file" | "pg" | "static"
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=f"{prefix}*", count=100)
                for k in keys:
                    key_id = k[len(prefix):]    # la parte dopo il prefisso
                    found.append({
                        "redis_key": k,
                        "loader":    loader,
                        "key_id":    key_id,
                    })
                    by_loader[loader] = by_loader.get(loader, 0) + 1
                    if len(found) >= limit:
                        break
                if cursor == 0 or len(found) >= limit:
                    break

        return {"keys": found, "count": len(found), "by_loader": by_loader}
    except Exception as e:
        logger.error("list_cached_api_keys error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_cached_api_key(key_id: str, loader: Optional[str] = None) -> dict:
    """
    Recupera i dettagli di una API key dalla cache Redis.
    Cerca in tutti i loader se 'loader' non è specificato.

    Args:
        key_id: SHA-256 hex (o ID raw) della chiave.
        loader: Loader specifico: 'file', 'pg', 'static'. None = cerca in tutti.

    Returns:
        dict con i campi della KeyRecord + 'redis_key', 'loader', 'ttl_seconds'.
    """
    r = _client()
    try:
        prefixes_to_try = (
            [f"stls:{loader}:key:"]
            if loader
            else _KEY_LOADER_PREFIXES
        )

        for prefix in prefixes_to_try:
            full_key = f"{prefix}{key_id}"
            raw = r.get(full_key)
            if raw is not None:
                ttl = r.ttl(full_key)
                data = _safe_json(raw)
                return {
                    "redis_key":   full_key,
                    "loader":      prefix.split(":")[1],
                    "key_id":      key_id,
                    "data":        data,
                    "ttl_seconds": ttl,
                }

        searched = [f"{p}{key_id}" for p in prefixes_to_try]
        return {
            "error":    f"Key '{key_id}' non trovata in cache Redis",
            "searched": searched,
        }
    except Exception as e:
        logger.error("get_cached_api_key error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def is_api_key_revoked(key_id: str) -> dict:
    """
    Verifica se una API key è nel SET dei revocati (stls:revoked).

    Args:
        key_id: SHA-256 hex della chiave.

    Returns:
        dict con 'key_id', 'revoked' (bool).
    """
    r = _client()
    try:
        revoked = r.sismember("stls:revoked", key_id)
        return {"key_id": key_id, "revoked": bool(revoked)}
    except Exception as e:
        logger.error("is_api_key_revoked error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def list_revoked_key_ids() -> dict:
    """
    Elenca tutti i key_id presenti nel SET stls:revoked.

    Returns:
        dict con 'revoked_key_ids' (lista) e 'count'.
    """
    r = _client()
    try:
        members = r.smembers("stls:revoked")
        return {"revoked_key_ids": sorted(members), "count": len(members)}
    except Exception as e:
        logger.error("list_revoked_key_ids error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


# ══════════════════════════════════════════════════════════════════════════════
# REDIS SERVER INFO
# ══════════════════════════════════════════════════════════════════════════════

def get_redis_server_info() -> dict:
    """
    Restituisce informazioni sul server Redis: versione, memoria usata,
    client connessi, uptime, keyspace.

    Returns:
        dict con 'version', 'used_memory_human', 'connected_clients',
        'uptime_in_seconds', 'keyspace'.
    """
    r = _client()
    try:
        info = r.info()
        keyspace = r.info("keyspace")
        return {
            "version": info.get("redis_version"),
            "used_memory_human": info.get("used_memory_human"),
            "connected_clients": info.get("connected_clients"),
            "uptime_in_seconds": info.get("uptime_in_seconds"),
            "total_commands_processed": info.get("total_commands_processed"),
            "keyspace": keyspace,
        }
    except Exception as e:
        logger.error("get_redis_server_info error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def cleanup_stale_index_entries() -> dict:
    """
    Rimuove da stls:index tutte le entry scadute (score <= now).
    Equivale a chiamare RedisTaskStore.cleanup_index() via agent.
    Sicuro da eseguire in qualsiasi momento — operazione O(log n + k).

    Returns:
        dict con 'removed' (int), 'remaining' (int), 'index_key'.
    """
    r = _client()
    try:
        now = time.time()
        removed = r.zremrangebyscore("stls:index", "-inf", now)
        remaining = r.zcard("stls:index")
        return {
            "removed": removed,
            "remaining": remaining,
            "index_key": "stls:index",
        }
    except Exception as e:
        logger.error("cleanup_stale_index_entries error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def count_keys_by_namespace() -> dict:
    """
    Conta le chiavi per ciascun namespace STLS-A2A:
    stls:task:*, stls:ctx:*, stls:key:*.
    Riporta anche la cardinalità di stls:index e stls:revoked.

    Returns:
        dict con i conteggi per namespace.
    """
    r = _client()
    try:
        counts: dict[str, int] = {}
        for pattern, label in [
            ("stls:task:*",       "tasks"),
            ("stls:ctx:*",        "contexts"),
            (f"{_SC_PREFIX}:*",   "stls_sessions"),
            ("stls:file:key:*",   "api_keys_file"),
            ("stls:pg:key:*",     "api_keys_pg"),
            ("stls:static:key:*", "api_keys_static"),
        ]:
            n = 0
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=pattern, count=200)
                n += len(keys)
                if cursor == 0:
                    break
            counts[label] = n

        counts["index_entries"] = r.zcard("stls:index")
        counts["revoked_keys"] = r.scard("stls:revoked")
        return counts
    except Exception as e:
        logger.error("count_keys_by_namespace error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()