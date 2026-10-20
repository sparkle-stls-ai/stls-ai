"""
tools_api_keys.py
-----------------
Tools per sofia_api_keys_agent.
Gestisce le API key nella tabella sf_api_keys su PostgreSQL.
Sincronizza revoche nella cache Redis (stls:revoked, stls:key:{sha256}).

Env vars attese:
  AUTH_PG_DSN   postgresql://user:pass@host:5432/dbname
  REDIS_URL     redis://localhost:6379   (per invalidazione cache)
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

# ── Costanti ───────────────────────────────────────────────────────────────────

_KEY_PREFIX = "stls_"       # prefisso leggibile per il raw key
_KEY_BYTES  = 32            # 32 bytes → 64 hex chars → raw key ~69 chars con prefisso


# ── Connessioni ────────────────────────────────────────────────────────────────

def _pg():
    dsn = os.environ.get("AUTH_PG_DSN")
    if not dsn:
        raise RuntimeError("AUTH_PG_DSN non configurata")
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
    Genera (raw_key, key_id).
    raw_key = stls_<64 hex chars>   → da consegnare al client UNA SOLA VOLTA
    key_id  = SHA-256(raw_key)      → stored su DB e Redis
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
    Elenca le API key registrate nel database PostgreSQL.

    Args:
        client_id:       Filtra per client_id. None = tutte.
        include_revoked: Se True, include anche le chiavi revocate (default False).
        limit:           Numero massimo di righe (default 50).

    Returns:
        dict con 'keys' (lista) e 'count'. Il campo 'key_id' è il SHA-256 (sicuro da mostrare).
        Il raw_key NON viene mai restituito da questa funzione.
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
    Recupera i dettagli completi di una API key tramite il suo key_id (SHA-256).

    Args:
        key_id: SHA-256 hex della chiave.

    Returns:
        dict con tutti i campi della chiave (mai il raw_key).
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
            return {"error": f"Key '{key_id}' non trovata"}
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
    Crea una nuova API key per il client specificato e la persiste su PostgreSQL.
    ATTENZIONE: il raw_key viene restituito UNA SOLA VOLTA. Non viene salvato.

    Args:
        client_id:    Identificatore del client/agent (es. 'agent-calculator').
        scopes:       Lista di scope autorizzati (es. ['read', 'write']).
        description:  Descrizione leggibile della chiave.
        endpoints:    Lista di endpoint autorizzati. Lista vuota = wildcard (tutti).
        expires_days: Giorni prima della scadenza. None = nessuna scadenza.
        metadata:     Dict JSON con informazioni aggiuntive opzionali.

    Returns:
        dict con 'key_id' (SHA-256), 'raw_key' (da consegnare al client),
        'client_id', 'scopes', 'expires_at'.
        Il raw_key è mostrato SOLO in questa risposta.
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

        logger.info("API key creata: key_id=%s client_id=%s", key_id, client_id)
        return {
            "key_id": key_id,
            "raw_key": raw_key,          # ← consegnare al client, poi non è recuperabile
            "client_id": client_id,
            "scopes": scopes,
            "endpoints": endpoints or [],
            "expires_at": expires_at.isoformat() if expires_at else None,
            "description": description,
            "warning": "Salva il raw_key ora: non sarà più recuperabile.",
        }
    except Exception as e:
        logger.error("create_api_key error: %s", e)
        return {"error": str(e)}


def revoke_api_key(key_id: str, reason: str = "") -> dict:
    """
    Revoca una API key: la marca come revocata su PostgreSQL e la aggiunge
    al SET stls:revoked su Redis per blocco immediato (senza attendere TTL cache).

    Args:
        key_id: SHA-256 hex della chiave da revocare.
        reason: Motivo della revoca (opzionale, salvato in metadata).

    Returns:
        dict con 'key_id', 'revoked' (bool), 'redis_updated' (bool).
    """
    try:
        # 1. Aggiorna PostgreSQL
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
            return {"error": f"Key '{key_id}' non trovata"}
        con.commit()
        cur.close()
        con.close()

        # 2. Aggiungi a stls:revoked su Redis (blocco immediato)
        redis_updated = False
        try:
            r = _redis_client()
            r.sadd("stls:revoked", key_id)
            # Invalida la cache per tutti i loader
            for prefix in ("stls:file:key:", "stls:pg:key:", "stls:static:key:"):
                r.delete(f"{prefix}{key_id}")
            r.close()
            redis_updated = True
        except Exception as re:
            logger.warning("Redis update failed during revocation: %s", re)

        logger.info("API key revocata: key_id=%s reason=%s", key_id, reason)
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
    Aggiorna gli scope (e opzionalmente gli endpoint) di una API key esistente.
    La cache Redis viene invalidata automaticamente.

    Args:
        key_id:    SHA-256 hex della chiave.
        scopes:    Nuova lista di scope.
        endpoints: Nuova lista di endpoint (None = non modificare).

    Returns:
        dict con i campi aggiornati.
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
            return {"error": f"Key '{key_id}' non trovata o già revocata"}
        con.commit()
        cur.close()
        con.close()

        # Invalida cache Redis per tutti i loader
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
    Restituisce statistiche aggregate sulle API key:
    totale, attive, revocate, scadute, per client.

    Returns:
        dict con 'total', 'active', 'revoked', 'expired', 'by_client'.
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