"""
stls_core_auth_models.py
-----------------------
Shared data model for the STLS API key authentication system.

No external dependencies — stdlib only. Importable from every auth
module with no circular import risk.
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
# Enumerations
# ---------------------------------------------------------------------------

class AuthResult(Enum):
    """Outcome of an API key validation."""
    OK = "ok"
    INVALID = "invalid"          # key not found or hash mismatch
    REVOKED = "revoked"          # key explicitly revoked
    EXPIRED = "expired"          # expires_at is in the past
    SCOPE_DENIED = "scope_denied"    # endpoint not authorized for this key
    INTERNAL_ERROR = "internal_error"


class LoaderBackend(Enum):
    """Backend configurable via the AUTH_LOADER_BACKEND env var."""
    STATIC = "static"       # in-memory, compatible with the current AuthConfig
    FILE = "file"           # YAML/JSON from AUTH_KEYS_FILE
    POSTGRES = "postgres"   # asyncpg, dsn from AUTH_PG_DSN


# ---------------------------------------------------------------------------
# KeyRecord — the main record of an API key
# ---------------------------------------------------------------------------

@dataclass
class KeyRecord:
    """
    Representation of an API key in the system.

    The ``key_id`` field always holds the SHA-256 hash of the raw key
    value — the plaintext is never persisted nor logged.

    Attributes:
        key_id:      SHA-256 hex digest of the raw key value (never the plaintext).
        client_id:   Client/agent identifier (e.g. "agent-weather").
        scopes:      List of authorized scopes (e.g. ["secure-a2a", "admin"]).
        endpoints:   Authorized endpoints (e.g. ["/secure-a2a"]).
                     Empty list = all endpoints authorized.
        expires_at:  UTC expiry. None = never expires.
        created_at:  UTC creation timestamp.
        description: Human-readable note, optional.
        metadata:    Arbitrary dict for application use (e.g. tenant, plan).
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
    # State validation
    # ------------------------------------------------------------------

    def is_expired(self) -> bool:
        """True if the key has an expiry and it has already passed."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def allows_endpoint(self, endpoint: str) -> bool:
        """
        True if this key is authorized for the requested endpoint.

        The check is prefix-based: a key with endpoints=["/secure-a2a"]
        also authorizes "/secure-a2a/stream". An empty list means wildcard.
        """
        if not self.endpoints:
            return True
        norm = endpoint.rstrip("/")
        return any(norm == e.rstrip("/") or norm.startswith(e.rstrip("/") + "/")
                   for e in self.endpoints)

    def has_scope(self, scope: str) -> bool:
        """True if this key has the requested scope or the '*' wildcard."""
        return "*" in self.scopes or scope in self.scopes

    # ------------------------------------------------------------------
    # Serialization / deserialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Converts to a JSON-serializable dict (for Redis, logs, API)."""
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
        """Reconstructs a KeyRecord from a dict (from Redis JSON, a PG row, YAML)."""
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
# AuditEntry — record of every authentication attempt
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    """
    Single audit log entry for every call to validate().

    Never contains the key's plaintext. Structured to be easily
    serializable to JSON and sent to SIEM systems.
    """

    timestamp: datetime
    key_id: str | None          # None if the key was malformed / absent
    client_id: str | None
    endpoint: str
    result: AuthResult
    remote_addr: str = ""
    request_id: str = ""        # trace id from the request, if available

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
# ValidationResult — output of ApiKeyManager.validate()
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Full validation outcome returned by ApiKeyManager.

    Attributes:
        result:  Outcome code (AuthResult).
        record:  KeyRecord if result == OK, None otherwise.
        audit:   Corresponding AuditEntry, always present.
    """

    result: AuthResult
    record: KeyRecord | None
    audit: AuditEntry

    @property
    def ok(self) -> bool:
        return self.result == AuthResult.OK

    def www_authenticate_header(self, realm: str = "STLS") -> str:
        """
        Value of the WWW-Authenticate header for 401 responses.
        Follows RFC 7235.
        """
        if self.result == AuthResult.EXPIRED:
            return f'ApiKey realm="{realm}", error="token_expired"'
        if self.result == AuthResult.REVOKED:
            return f'ApiKey realm="{realm}", error="token_revoked"'
        if self.result == AuthResult.SCOPE_DENIED:
            return f'ApiKey realm="{realm}", error="insufficient_scope"'
        return f'ApiKey realm="{realm}"'


# ---------------------------------------------------------------------------
# Cryptographic utilities — used by the manager and the loaders
# ---------------------------------------------------------------------------

def hash_key(raw_key: str) -> str:
    """
    Computes the SHA-256 hex digest of the raw API key value.

    This is the only function that receives the plaintext. Always use
    it as the first operation, then discard the raw value.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    """
    Timing-safe comparison between two strings (wrapper over hmac.compare_digest).
    Prevents timing attacks on key validation.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_api_key(prefix: str = "stls") -> tuple[str, str]:
    """
    Generates a new secure API key.

    Returns:
        (raw_key, key_id): the plaintext to deliver to the client
        and the key_id (SHA-256) to persist.

    Format: ``{prefix}_{32 random hex bytes}``
    Entropy: 256 bits.
    """
    raw = f"{prefix}_{secrets.token_hex(32)}"
    return raw, hash_key(raw)


# ---------------------------------------------------------------------------
# AuthConfig — lightweight static validation, no loader/cache/refresh
# ---------------------------------------------------------------------------

@dataclass
class AuthConfig:
    """
    Static, in-memory authorization configuration, with no loader/
    Redis/refresh — meant for tests, standalone scripts and small
    deployments where ApiKeyManager's full lifecycle (loader,
    RedisKeyCache, BackgroundRefresher, async start/stop) is
    unnecessary ceremony.

    Originally a local class in stls-a2a (a pydantic BaseModel), moved
    here to stls-core to actually be shared with stls-mcp without
    duplication — unlike KeyRecord/ApiKeyManager, it requires no
    external dependency (redis/yaml/asyncpg stay lazy here too, as in
    the rest of this file), so it introduces no new import requirement
    for anyone already using ApiKeyManager: it's a pure stdlib
    dataclass, consistent with KeyRecord/AuditEntry in this same module.

    Supports two schemes, usable together or separately:
      - Bearer token : header  Authorization: Bearer <token>
      - API Key      : header  X-API-Key: <key>
                       or      query param  ?api_key=<key>

    If both sets are empty, no validation is performed (backward
    compatibility: AuthConfig() leaves the endpoints open).
    The comparison uses constant_time_equal() (hmac.compare_digest)
    to prevent timing attacks.

    Does not replace ApiKeyManager: where key rotation without
    restarts, structured audit, per-key scope/endpoint, or a shared
    Redis/File/Postgres backend across multiple instances is needed,
    use ApiKeyManager instead. AuthConfig remains the "two lines for a
    local test" fallback — lower priority than ApiKeyManager when both
    are configured (see the _check_auth_async pattern in stls-a2a and
    the ASGI gate in stls-mcp: ApiKeyManager, when present, always
    takes precedence).

    Example:
        AuthConfig(api_keys={"key-abc123"}, realm="My Server")
    """
    bearer_tokens: set[str] = field(default_factory=set)
    api_keys: set[str] = field(default_factory=set)
    realm: str = "STLS"

    @property
    def enabled(self) -> bool:
        return bool(self.bearer_tokens or self.api_keys)

    def check(self, authorization: str | None, api_key: str | None) -> bool:
        """Verifies the given credentials. True if at least one scheme is valid."""
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
# Configuration constants (defaults — overridable via env)
# ---------------------------------------------------------------------------

DEFAULT_KEY_TTL_SECONDS: int = 3600          # Redis TTL for KeyRecords
DEFAULT_REFRESH_INTERVAL_SECONDS: int = 300  # BackgroundRefresher interval

REDIS_KEY_PREFIX = "stls:key:"              # stls:key:{key_id}
REDIS_REVOKED_SET = "stls:revoked"          # SET of revoked key_ids
REDIS_INDEX_SET = "stls:keys:index"         # SET of all known key_ids
