"""
tools_redis.py
--------------
Tools for sofia_redis_agent.
Navigates STLS-A2A's key namespaces on Redis:
  - stls:task:{id}        → task data (JSON)
  - stls:ctx:{context_id} → context data (JSON)
  - stls:index            → sorted set of task IDs
  - stls:key:{sha256}     → API key record (JSON)
  - stls:revoked          → SET of revoked key_ids

Expected env vars (with defaults):
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

# ── Connection ────────────────────────────────────────────────────────────────

def _client() -> _redis.Redis:
    """Returns a sync Redis client (not pooled, uses short-lived connections)."""
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
    Lists the task IDs present in the Redis index (stls:index).

    Args:
        limit:        Maximum number of tasks to return (default 50).
        offset:       Offset for pagination (default 0).
        newest_first: If True, sorts from most recent (descending score).

    Returns:
        dict with 'task_ids' (list), 'total' (int in the sorted set), 'offset', 'limit'.
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
    Retrieves a task's full payload from Redis.

    Args:
        task_id: the task's ID (e.g. "abc123").

    Returns:
        dict with the task's content, or {"error": "..."} if not found.
    """
    r = _client()
    try:
        raw = r.get(f"stls:task:{task_id}")
        if raw is None:
            return {"error": f"Task '{task_id}' not found"}
        data = _safe_json(raw)
        return {"task_id": task_id, "data": data}
    except Exception as e:
        logger.error("get_task error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def get_task_context(context_id: str) -> dict:
    """
    Retrieves the context associated with a task (stls:ctx:{context_id}).

    Args:
        context_id: the context's ID.

    Returns:
        dict with the context's data.
    """
    r = _client()
    try:
        raw = r.get(f"stls:ctx:{context_id}")
        if raw is None:
            return {"error": f"Context '{context_id}' not found"}
        return {"context_id": context_id, "data": _safe_json(raw)}
    except Exception as e:
        logger.error("get_task_context error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def search_tasks_by_state(state: str, limit: int = 20) -> dict:
    """
    Searches for tasks filtered by state (e.g. 'working', 'completed', 'failed', 'submitted').
    Scans the stls:task:* keys and filters by the 'status.state' field.

    Args:
        state: the state to search for (e.g. 'working', 'completed', 'failed', 'submitted').
        limit: maximum number of results (default 20).

    Returns:
        dict with 'tasks' (list of {task_id, state, context_id}) and 'count'.
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
    Returns aggregate statistics on tasks: total in the index,
    count by state, average TTL.

    Returns:
        dict with 'total_in_index', 'counts_by_state', 'total_scanned'.
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
# Structure of each entry, as of stls_core 0.1.8 (issue #20 — no longer
# a raw "session_key" field; the cache now persists only the already-
# derived AES/HMAC keys, which these tools never read anyway):
#   {"enc_aes_key": "<base64>", "dec_aes_key": "<base64>",
#    "enc_hmac_key": "<base64>", "dec_hmac_key": "<base64>",
#    "kid": "<kid>", "created_at": <ts>}
# TTL: sliding (GETEX), default 3600s (SESSION_CACHE_TTL)
#
# No raw key material is ever returned by these tools, for security —
# only 'kid' and 'created_at' below.
#
# If STLS_CORE_ATREST_KEY is set on the stls_core side, the whole entry
# is instead an opaque AES-256-GCM blob, not JSON: _safe_json() then
# returns {"raw": raw}, so 'kid'/'created_at' below silently come back
# empty/None (provider hint falls through to "other") rather than
# erroring. This module has no access to that KEK and makes no attempt
# to decrypt — introspecting sealed sessions isn't supported here.
#
# The 'kid' lets us estimate the provider:
#   UUID format    → pqc   (every PQC handshake generates a fresh UUID)
#   base-session-* → base
#   other          → arqit_py


def list_stls_sessions(
    limit: int = 50,
    offset: int = 0,
    provider_filter: Optional[str] = None,
) -> dict:
    """
    Lists the active STLS sessions in the RedisSessionCache.

    Args:
        limit:           Maximum number of sessions to return (default 50).
        offset:          Offset for pagination (default 0).
        provider_filter: Filter by estimated provider: 'pqc', 'arqit_py', 'base'.
                         None = all.

    Returns:
        dict with 'sessions' (list), 'count', 'total_found', 'namespace'.
        Each session includes: session_id, kid, provider_hint, created_at, ttl_seconds.
        The session_key field is NOT included, for security.
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
    Retrieves the metadata of a specific STLS session.
    The session_key field is NOT included, for security.

    Args:
        session_id: the session's ID (UUID or other format).

    Returns:
        dict with session_id, kid, provider_hint, created_at, ttl_seconds.
    """
    r = _client()
    try:
        key = f"{_SC_PREFIX}:{session_id}"
        raw = r.get(key)
        if raw is None:
            return {"error": f"Session '{session_id}' not found (expired or nonexistent)"}

        data = _safe_json(raw)
        if not isinstance(data, dict):
            return {"error": "Invalid session data", "raw": raw}

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
    Aggregate statistics on the STLS sessions active in Redis.

    Returns:
        dict with total, count by provider_hint (pqc/arqit_py/base),
        avg/min/max TTL, oldest and most recent session.
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
    Deletes an STLS session from the Redis cache, forcing the client to redo
    the handshake. Use this to invalidate compromised sessions or expire
    them early.

    Args:
        session_id: the ID of the session to delete.

    Returns:
        dict with 'deleted' (bool) and 'session_id'.
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


# Per-loader prefixes — stls:{loader}:key:{sha256}
_KEY_LOADER_PREFIXES = [
    "stls:file:key:",
    "stls:pg:key:",
    "stls:static:key:",
]

# Per-loader indices
_KEY_INDEX_KEYS = [
    "stls:file:keys:index",
    "stls:pg:keys:index",
    "stls:static:keys:index",
    "stls:keys:index",          # possible global index
]

# Session cache prefix — matches SESSION_CACHE_REDIS_NAMESPACE (default stls:sc)
_SC_PREFIX = os.environ.get("SESSION_CACHE_REDIS_NAMESPACE", "stls:sc")

# Regex to tell PQC (UUID kid) apart from arqit_py / base
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _provider_hint(kid: str) -> str:
    """
    Estimates the provider from the kid's format:
    - UUID → pqc (every handshake generates a fresh UUID)
    - 'base-session-key-identifier' → base
    - other → arqit_py
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
    Lists the API keys present in the Redis cache for all loaders
    (stls:file:key:*, stls:pg:key:*, stls:static:key:*).

    Args:
        limit: Maximum number of keys to return (default 50).

    Returns:
        dict with 'keys' (list of {redis_key, loader, key_id}), 'count',
        'by_loader' (count per loader).
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
                    key_id = k[len(prefix):]    # the part after the prefix
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
    Retrieves an API key's details from the Redis cache.
    Searches across all loaders if 'loader' isn't specified.

    Args:
        key_id: the key's SHA-256 hex (or raw ID).
        loader: specific loader: 'file', 'pg', 'static'. None = search all.

    Returns:
        dict with the KeyRecord's fields + 'redis_key', 'loader', 'ttl_seconds'.
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
            "error":    f"Key '{key_id}' not found in Redis cache",
            "searched": searched,
        }
    except Exception as e:
        logger.error("get_cached_api_key error: %s", e)
        return {"error": str(e)}
    finally:
        r.close()


def is_api_key_revoked(key_id: str) -> dict:
    """
    Checks whether an API key is in the revoked SET (stls:revoked).

    Args:
        key_id: the key's SHA-256 hex.

    Returns:
        dict with 'key_id', 'revoked' (bool).
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
    Lists all key_ids present in the stls:revoked SET.

    Returns:
        dict with 'revoked_key_ids' (list) and 'count'.
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
    Returns information about the Redis server: version, memory used,
    connected clients, uptime, keyspace.

    Returns:
        dict with 'version', 'used_memory_human', 'connected_clients',
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
    Removes all expired entries (score <= now) from stls:index.
    Equivalent to calling RedisTaskStore.cleanup_index() via the agent.
    Safe to run at any time — an O(log n + k) operation.

    Returns:
        dict with 'removed' (int), 'remaining' (int), 'index_key'.
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
    Counts the keys for each STLS-A2A namespace:
    stls:task:*, stls:ctx:*, stls:key:*.
    Also reports the cardinality of stls:index and stls:revoked.

    Returns:
        dict with the counts per namespace.
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