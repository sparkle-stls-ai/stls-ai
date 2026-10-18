"""
stls_core_auth_models.py
-----------------------
Data model condiviso per il sistema di autenticazione API key di STLS.

Nessuna dipendenza esterna — solo stdlib. Importabile da tutti i moduli
auth senza rischio di circular import.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerazioni
# ---------------------------------------------------------------------------

class AuthResult(Enum):
    """Esito della validazione di una API key."""
    OK = "ok"
    INVALID = "invalid"          # key non trovata o hash non corrisponde
    REVOKED = "revoked"          # key esplicitamente revocata
    EXPIRED = "expired"          # expires_at nel passato
    SCOPE_DENIED = "scope_denied"    # endpoint non autorizzato per questa key
    INTERNAL_ERROR = "internal_error"


class LoaderBackend(Enum):
    """Backend configurabile via env AUTH_LOADER_BACKEND."""
    STATIC = "static"       # in-memory, compatibile con AuthConfig attuale
    FILE = "file"           # YAML/JSON da AUTH_KEYS_FILE
    POSTGRES = "postgres"   # asyncpg, dsn da AUTH_PG_DSN


# ---------------------------------------------------------------------------
# KeyRecord — record principale di una API key
# ---------------------------------------------------------------------------

@dataclass
class KeyRecord:
    """
    Rappresentazione di una API key nel sistema.

    Il campo ``key_id`` contiene sempre l'hash SHA-256 del valore grezzo
    della chiave — il plaintext non viene mai persistito né loggato.

    Attributes:
        key_id:      SHA-256 hex digest del valore grezzo (mai il plaintext).
        client_id:   Identificativo del client/agente (es. "agent-weather").
        scopes:      Lista di scope autorizzati (es. ["secure-a2a", "admin"]).
        endpoints:   Endpoint autorizzati (es. ["/secure-a2a"]).
                     Lista vuota = tutti gli endpoint autorizzati.
        expires_at:  Scadenza UTC. None = nessuna scadenza.
        created_at:  Timestamp di creazione UTC.
        description: Nota leggibile, opzionale.
        metadata:    Dict arbitrario per uso applicativo (es. tenant, piano).
    """

    key_id: str
    client_id: str
    scopes: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Validazione dello stato
    # ------------------------------------------------------------------

    def is_expired(self) -> bool:
        """True se la key ha una scadenza e questa è già trascorsa."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def allows_endpoint(self, endpoint: str) -> bool:
        """
        True se questa key è autorizzata per l'endpoint richiesto.

        La verifica è prefix-based: una key con endpoint=["/secure-a2a"]
        autorizza anche "/secure-a2a/stream". Lista vuota = wildcard.
        """
        if not self.endpoints:
            return True
        norm = endpoint.rstrip("/")
        return any(norm == e.rstrip("/") or norm.startswith(e.rstrip("/") + "/")
                   for e in self.endpoints)

    def has_scope(self, scope: str) -> bool:
        """True se la key ha lo scope richiesto o il wildcard '*'."""
        return "*" in self.scopes or scope in self.scopes

    # ------------------------------------------------------------------
    # Serializzazione / deserializzazione
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Converte in dict JSON-serializzabile (per Redis, log, API)."""
        return {
            "key_id": self.key_id,
            "client_id": self.client_id,
            "scopes": self.scopes,
            "endpoints": self.endpoints,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat(),
            "description": self.description,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KeyRecord":
        """Ricostruisce un KeyRecord da un dict (da Redis JSON, PG row, YAML)."""
        expires_at = None
        if data.get("expires_at"):
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

        created_at = datetime.now(timezone.utc)
        if data.get("created_at"):
            created_at = datetime.fromisoformat(data["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

        return cls(
            key_id=data["key_id"],
            client_id=data["client_id"],
            scopes=list(data.get("scopes", [])),
            endpoints=list(data.get("endpoints", [])),
            expires_at=expires_at,
            created_at=created_at,
            description=data.get("description", ""),
            metadata=dict(data.get("metadata", {})),
        )

    def __repr__(self) -> str:
        exp = self.expires_at.isoformat() if self.expires_at else "never"
        return (
            f"KeyRecord(client_id={self.client_id!r}, "
            f"key_id={self.key_id[:12]}..., "
            f"scopes={self.scopes}, expires={exp})"
        )


# ---------------------------------------------------------------------------
# AuditEntry — record di ogni tentativo di autenticazione
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    """
    Singola voce del log di audit per ogni chiamata a validate().

    Non contiene mai il plaintext della chiave. Strutturato per essere
    facilmente serializzabile in JSON e inviato a sistemi SIEM.
    """

    timestamp: datetime
    key_id: str | None          # None se la key era malformata / assente
    client_id: str | None
    endpoint: str
    result: AuthResult
    remote_addr: str = ""
    request_id: str = ""        # trace id dalla request, se disponibile

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "key_id": self.key_id,
            "client_id": self.client_id,
            "endpoint": self.endpoint,
            "result": self.result.value,
            "remote_addr": self.remote_addr,
            "request_id": self.request_id,
        }


# ---------------------------------------------------------------------------
# ValidationResult — output di ApiKeyManager.validate()
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Esito completo della validazione restituito da ApiKeyManager.

    Attributes:
        result:  Codice esito (AuthResult).
        record:  KeyRecord se result == OK, None altrimenti.
        audit:   AuditEntry corrispondente, sempre presente.
    """

    result: AuthResult
    record: KeyRecord | None
    audit: AuditEntry

    @property
    def ok(self) -> bool:
        return self.result == AuthResult.OK

    def www_authenticate_header(self, realm: str = "STLS") -> str:
        """
        Valore dell'header WWW-Authenticate per risposte 401.
        Segue RFC 7235.
        """
        if self.result == AuthResult.EXPIRED:
            return f'ApiKey realm="{realm}", error="token_expired"'
        if self.result == AuthResult.REVOKED:
            return f'ApiKey realm="{realm}", error="token_revoked"'
        if self.result == AuthResult.SCOPE_DENIED:
            return f'ApiKey realm="{realm}", error="insufficient_scope"'
        return f'ApiKey realm="{realm}"'


# ---------------------------------------------------------------------------
# Utilità crittografiche — usate da manager e loaders
# ---------------------------------------------------------------------------

def hash_key(raw_key: str) -> str:
    """
    Calcola SHA-256 hex digest del valore grezzo della API key.

    È l'unica funzione che riceve il plaintext. Usarla sempre come
    prima operazione, poi scartare il valore raw.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    """
    Confronto timing-safe tra due stringhe (wrapper su hmac.compare_digest).
    Previene timing attacks sulla validazione delle chiavi.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_api_key(prefix: str = "stls") -> tuple[str, str]:
    """
    Genera una nuova API key sicura.

    Returns:
        (raw_key, key_id): il plaintext da consegnare al client
        e il key_id (SHA-256) da persistere.

    Formato: ``{prefix}_{32 byte hex random}``
    Entropia: 256 bit.
    """
    raw = f"{prefix}_{secrets.token_hex(32)}"
    return raw, hash_key(raw)


# ---------------------------------------------------------------------------
# AuthConfig — validazione statica leggera, senza loader/cache/refresh
# ---------------------------------------------------------------------------

@dataclass
class AuthConfig:
    """
    Configurazione statica dell'autorizzazione, in-memory, senza loader/
    Redis/refresh — pensata per test, script standalone e deployment
    piccoli dove il ciclo di vita completo di ApiKeyManager (loader,
    RedisKeyCache, BackgroundRefresher, avvio/arresto async) è cerimonia
    non necessaria.

    Nata in stls-a2a come classe locale (pydantic BaseModel), spostata qui
    in stls-core per essere condivisa senza duplicazione con stls-mcp — a
    differenza di KeyRecord/ApiKeyManager, non richiede alcuna dipendenza
    esterna (redis/yaml/asyncpg restano comunque lazy, come nel resto di
    questo file), quindi non introduce alcun nuovo requisito di import
    per chi usa già ApiKeyManager: è dataclass stdlib pura, coerente con
    KeyRecord/AuditEntry in questo stesso modulo.

    Supporta due schemi, usabili insieme o separatamente:
      - Bearer token : header  Authorization: Bearer <token>
      - API Key      : header  X-API-Key: <key>
                       oppure  query param  ?api_key=<key>

    Se entrambi i set sono vuoti nessuna validazione viene effettuata
    (retrocompatibilità: AuthConfig() lascia gli endpoint aperti).
    Il confronto avviene con constant_time_equal() (hmac.compare_digest)
    per prevenire timing attacks.

    Non sostituisce ApiKeyManager: dove serve rotazione delle chiavi senza
    riavvii, audit strutturato, scope/endpoint per-key, o un backend
    Redis/File/Postgres condiviso tra più istanze, usare ApiKeyManager.
    AuthConfig resta il fallback "due righe per un test locale" — priorità
    inversa rispetto ad ApiKeyManager quando entrambi sono configurati
    (vedi il pattern _check_auth_async in stls-a2a e il gate ASGI in
    stls-mcp: ApiKeyManager, se presente, ha sempre precedenza).

    Esempio:
        AuthConfig(api_keys={"key-abc123"}, realm="My Server")
    """
    bearer_tokens: set[str] = field(default_factory=set)
    api_keys: set[str] = field(default_factory=set)
    realm: str = "STLS"

    @property
    def enabled(self) -> bool:
        return bool(self.bearer_tokens or self.api_keys)

    def check(self, authorization: str | None, api_key: str | None) -> bool:
        """Verifica le credenziali fornite. True se almeno uno schema è valido."""
        if self.bearer_tokens and authorization:
            parts = authorization.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                for token in self.bearer_tokens:
                    if constant_time_equal(parts[1], token):
                        return True

        if self.api_keys and api_key:
            for key in self.api_keys:
                if constant_time_equal(api_key, key):
                    return True

        return False


# ---------------------------------------------------------------------------
# Costanti di configurazione (valori di default — sovrascrivibili via env)
# ---------------------------------------------------------------------------

DEFAULT_KEY_TTL_SECONDS: int = 3600          # TTL Redis per i KeyRecord
DEFAULT_REFRESH_INTERVAL_SECONDS: int = 300  # intervallo BackgroundRefresher

REDIS_KEY_PREFIX = "stls:key:"              # stls:key:{key_id}
REDIS_REVOKED_SET = "stls:revoked"          # SET dei key_id revocati
REDIS_INDEX_SET = "stls:keys:index"         # SET di tutti i key_id noti