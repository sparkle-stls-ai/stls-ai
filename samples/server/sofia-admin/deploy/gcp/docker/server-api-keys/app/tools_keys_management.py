"""
tools_api_keys.py
-----------------
Tools for sofia_api_keys_agent.
Manages API keys in the sf_api_keys table on PostgreSQL.
Syncs revocations to the Redis cache (stls:revoked, stls:key:{sha256}).

Expected env vars:
  AUTH_PG_DSN   postgresql://user:pass@host:5432/dbname
  REDIS_URL     redis://localhost:6379   (for cache invalidation)
  REDIS_DB      0
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import redis as _redis

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────────

_KEY_PREFIX = "stls_"       # readable prefix for the raw key
_KEY_BYTES  = 32            # 32 bytes → 64 hex chars → raw key ~69 chars with prefix


# ── Connections ────────────────────────────────────────────────────────────────

def _pg():
    dsn = os.environ.get("AUTH_PG_DSN")
    if not dsn:
        raise RuntimeError("AUTH_PG_DSN not configured")
    return psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)


def _redis_client() -> _redis.Redis:
    return _redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379"),
        db=int(os.environ.get("REDIS_DB", 0)),
        decode_responses=True,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _generate_api_key() -> tuple[str, str]:
    """
    Generates (raw_key, key_id).
    raw_key = stls_<64 hex chars>   → to be handed to the client ONLY ONCE
    key_id  = SHA-256(raw_key)      → stored in DB and Redis
    """
    raw = _KEY_PREFIX + secrets.token_hex(_KEY_BYTES)
    kid = hashlib.sha256(raw.encode()).hexdigest()
    return raw, kid


def _rows_to_list(cur) -> list[dict]:
    rows = cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for field in ("expires_at", "created_at"):
            if d.get(field):
                d[field] = d[field].isoformat()
        result.append(d)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# API KEY MANAGEMENT TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def list_api_keys(
    client_id: Optional[str] = None,
    include_revoked: bool = False,
    limit: int = 50,
) -> dict:
    """
    Lists the API keys registered in the PostgreSQL database.

    Args:
        client_id:       Filter by client_id. None = all.
        include_revoked: If True, also include revoked keys (default False).
        limit:           Maximum number of rows (default 50).

    Returns:
        dict with 'keys' (list) and 'count'. The 'key_id' field is the SHA-256 (safe to show).
        The raw_key is NEVER returned by this function.
    """
    try:
        con = _pg()
        cur = con.cursor()
        conditions = []
        params: list = []

        if client_id:
            conditions.append("client_id = %s")
            params.append(client_id)
        if not include_revoked:
            conditions.append("revoked = false")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(
            f"""
            SELECT key_id, client_id, scopes, endpoints,
                   expires_at, created_at, revoked, description,
                   metadata
            FROM sf_api_keys
            {where}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params + [limit],
        )
        keys = _rows_to_list(cur)
        cur.close()
        con.close()
        return {"keys": keys, "count": len(keys)}
    except Exception as e:
        logger.error("list_api_keys error: %s", e)
        return {"error": str(e)}


def get_api_key_details(key_id: str) -> dict:
    """
    Retrieves the full details of an API key by its key_id (SHA-256).

    Args:
        key_id: SHA-256 hex of the key.

    Returns:
        dict with all the key's fields (never the raw_key).
    """
    try:
        con = _pg()
        cur = con.cursor()
        cur.execute(
            """
            SELECT key_id, client_id, scopes, endpoints,
                   expires_at, created_at, revoked, description, metadata
            FROM sf_api_keys
            WHERE key_id = %s
            """,
            (key_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": f"Key '{key_id}' not found"}
        data = dict(row)
        for field in ("expires_at", "created_at"):
            if data.get(field):
                data[field] = data[field].isoformat()
        cur.close()
        con.close()
        return data
    except Exception as e:
        logger.error("get_api_key_details error: %s", e)
        return {"error": str(e)}


def create_api_key(
    client_id: str,
    scopes: list[str],
    description: str = "",
    endpoints: Optional[list[str]] = None,
    expires_days: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Creates a new API key for the given client and persists it to PostgreSQL.
    WARNING: the raw_key is returned ONLY ONCE. It is not saved.

    Args:
        client_id:    Client/agent identifier (e.g. 'agent-calculator').
        scopes:       List of authorized scopes (e.g. ['read', 'write']).
        description:  Human-readable description of the key.
        endpoints:    List of authorized endpoints. Empty list = wildcard (all).
        expires_days: Days before expiry. None = no expiry.
        metadata:     Optional JSON dict with additional information.

    Returns:
        dict with 'key_id' (SHA-256), 'raw_key' (to hand to the client),
        'client_id', 'scopes', 'expires_at'.
        The raw_key is shown ONLY in this response.
    """
    try:
        raw_key, key_id = _generate_api_key()
        expires_at = None
        if expires_days is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

        con = _pg()
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO sf_api_keys
              (key_id, client_id, scopes, endpoints, expires_at, description, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                key_id,
                client_id,
                scopes,
                endpoints or [],
                expires_at,
                description,
                psycopg2.extras.Json(metadata or {}),
            ),
        )
        con.commit()
        cur.close()
        con.close()

        logger.info("API key created: key_id=%s client_id=%s", key_id, client_id)
        return {
            "key_id": key_id,
            "raw_key": raw_key,          # ← hand to the client, cannot be retrieved afterwards
            "client_id": client_id,
            "scopes": scopes,
            "endpoints": endpoints or [],
            "expires_at": expires_at.isoformat() if expires_at else None,
            "description": description,
            "warning": "Save the raw_key now: it won't be retrievable again.",
        }
    except Exception as e:
        logger.error("create_api_key error: %s", e)
        return {"error": str(e)}


def revoke_api_key(key_id: str, reason: str = "") -> dict:
    """
    Revokes an API key: marks it as revoked in PostgreSQL and adds it to
    the stls:revoked SET on Redis for immediate blocking (without waiting
    for the cache TTL).

    Args:
        key_id: SHA-256 hex of the key to revoke.
        reason: Reason for revocation (optional, saved in metadata).

    Returns:
        dict with 'key_id', 'revoked' (bool), 'redis_updated' (bool).
    """
    try:
        # 1. Update PostgreSQL
        con = _pg()
        cur = con.cursor()
        cur.execute(
            """
            UPDATE sf_api_keys
            SET revoked  = true,
                metadata = metadata || %s::jsonb
            WHERE key_id = %s
            RETURNING key_id, client_id, revoked
            """,
            (
                psycopg2.extras.Json({"revoked_reason": reason, "revoked_at": datetime.now(timezone.utc).isoformat()}),
                key_id,
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.close()
            con.close()
            return {"error": f"Key '{key_id}' not found"}
        con.commit()
        cur.close()
        con.close()

        # 2. Add to stls:revoked on Redis (immediate blocking)
        redis_updated = False
        try:
            r = _redis_client()
            r.sadd("stls:revoked", key_id)
            # Invalidate the cache for all loaders
            for prefix in ("stls:file:key:", "stls:pg:key:", "stls:static:key:"):
                r.delete(f"{prefix}{key_id}")
            r.close()
            redis_updated = True
        except Exception as re:
            logger.warning("Redis update failed during revocation: %s", re)

        logger.info("API key revoked: key_id=%s reason=%s", key_id, reason)
        return {
            "key_id": key_id,
            "client_id": dict(row).get("client_id"),
            "revoked": True,
            "redis_updated": redis_updated,
        }
    except Exception as e:
        logger.error("revoke_api_key error: %s", e)
        return {"error": str(e)}


def update_api_key_scopes(
    key_id: str,
    scopes: list[str],
    endpoints: Optional[list[str]] = None,
) -> dict:
    """
    Updates the scopes (and optionally the endpoints) of an existing API key.
    The Redis cache is invalidated automatically.

    Args:
        key_id:    SHA-256 hex of the key.
        scopes:    New list of scopes.
        endpoints: New list of endpoints (None = don't change).

    Returns:
        dict with the updated fields.
    """
    try:
        con = _pg()
        cur = con.cursor()

        if endpoints is not None:
            cur.execute(
                """
                UPDATE sf_api_keys SET scopes = %s, endpoints = %s
                WHERE key_id = %s AND revoked = false
                RETURNING key_id, client_id, scopes, endpoints
                """,
                (scopes, endpoints, key_id),
            )
        else:
            cur.execute(
                """
                UPDATE sf_api_keys SET scopes = %s
                WHERE key_id = %s AND revoked = false
                RETURNING key_id, client_id, scopes, endpoints
                """,
                (scopes, key_id),
            )

        row = cur.fetchone()
        if row is None:
            cur.close()
            con.close()
            return {"error": f"Key '{key_id}' not found or already revoked"}
        con.commit()
        cur.close()
        con.close()

        # Invalidate the Redis cache for all loaders
        try:
            r = _redis_client()
            for prefix in ("stls:file:key:", "stls:pg:key:", "stls:static:key:"):
                r.delete(f"{prefix}{key_id}")
            r.close()
        except Exception as re:
            logger.warning("Redis cache invalidation failed: %s", re)

        return {**dict(row), "updated": True}
    except Exception as e:
        logger.error("update_api_key_scopes error: %s", e)
        return {"error": str(e)}


def get_api_keys_stats() -> dict:
    """
    Returns aggregate statistics on API keys:
    total, active, revoked, expired, by client.

    Returns:
        dict with 'total', 'active', 'revoked', 'expired', 'by_client'.
    """
    try:
        con = _pg()
        cur = con.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*)                                                          AS total,
                COUNT(*) FILTER (WHERE NOT revoked AND (expires_at IS NULL OR expires_at > now())) AS active,
                COUNT(*) FILTER (WHERE revoked)                                   AS revoked,
                COUNT(*) FILTER (WHERE NOT revoked AND expires_at <= now())       AS expired
            FROM sf_api_keys
            """
        )
        stats = dict(cur.fetchone())

        cur.execute(
            """
            SELECT client_id,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT revoked) AS active
            FROM sf_api_keys
            GROUP BY client_id
            ORDER BY total DESC
            """
        )
        by_client = _rows_to_list(cur)
        cur.close()
        con.close()
        return {**stats, "by_client": by_client}
    except Exception as e:
        logger.error("get_api_keys_stats error: %s", e)
        return {"error": str(e)}