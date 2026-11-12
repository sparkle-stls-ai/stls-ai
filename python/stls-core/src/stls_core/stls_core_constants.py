"""
stls_core_constants.py
----------------------
Constants shared across all STLS packages (stls-core, stls-a2a, stls-mcp).

No external dependencies — stdlib only.
Importable from any module with no circular import risk.
"""

# ---------------------------------------------------------------------------
# STLS protocol version
# ---------------------------------------------------------------------------

STLS_VERSION: str = "1.0"
"""STLS protocol version included in every encrypted envelope."""


# ---------------------------------------------------------------------------
# HTTP headers
# ---------------------------------------------------------------------------

# STLS session identifiers
HEADER_SESSION_ID: str = "X-STLS-Session-ID"
"""Header carrying the STLS session_id on every encrypted request."""

HEADER_KID: str = "X-STLS-Kid"
"""Header carrying the current session's key identifier (kid)."""

# Authentication
HEADER_API_KEY: str = "X-API-Key"
"""Standard header for API key authentication."""

HEADER_AUTHORIZATION: str = "Authorization"
"""Standard HTTP header for a Bearer token."""

# Context metadata (used by stls-a2a, declared here to be shared)
HEADER_USER_ID: str = "X-STLS-User-Id"
"""Optional header to propagate the user_id in the agent's context."""

HEADER_PROFILE: str = "X-STLS-Profile"
"""Optional header to propagate the user profile in the agent's context."""


# ---------------------------------------------------------------------------
# Encrypted envelope fields
# ---------------------------------------------------------------------------

ENVELOPE_VERSION: str = "version"
ENVELOPE_SESSION_ID: str = "session_id"
ENVELOPE_IV: str = "iv"
ENVELOPE_CIPHERTEXT: str = "ciphertext"
ENVELOPE_TAG: str = "tag"
ENVELOPE_MAC: str = "mac"
"""
Keys of the JSON envelope produced by StlsTransport.encrypt().

Structure:
{
    "version":    "1.0",
    "session_id": "<uuid>",
    "iv":         "<base64 96-bit>",
    "ciphertext": "<base64>",
    "tag":        "<base64 128-bit GCM tag>",
    "mac":        "<base64 SHA-256 HMAC>"
}
"""


# ---------------------------------------------------------------------------
# HKDF domain separators
# ---------------------------------------------------------------------------

HKDF_INFO_AES: bytes = b"stls-aes-256-gcm"
"""Info label for deriving the AES key via HKDF-Expand (direction-less / legacy mode)."""

HKDF_INFO_HMAC: bytes = b"stls-hmac-sha256"
"""Info label for deriving the HMAC key via HKDF-Expand (direction-less / legacy mode)."""

HKDF_INFO_AES_C2S: bytes = b"stls-aes-256-gcm-c2s"
"""Info label for the client->server AES key, used only when StlsTransport is given a direction."""

HKDF_INFO_AES_S2C: bytes = b"stls-aes-256-gcm-s2c"
"""Info label for the server->client AES key, used only when StlsTransport is given a direction."""

HKDF_INFO_HMAC_C2S: bytes = b"stls-hmac-sha256-c2s"
"""Info label for the client->server HMAC key, used only when StlsTransport is given a direction."""

HKDF_INFO_HMAC_S2C: bytes = b"stls-hmac-sha256-s2c"
"""Info label for the server->client HMAC key, used only when StlsTransport is given a direction."""


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

ENV_KEY_PROVIDER_NAME: str = "KEY_PROVIDER_NAME"
"""
Name of the active key provider.
Supported values: "base" | "arqit_py" | "pqc"
Default: "base"
"""

ENV_KEY_SESSION_PORT: str = "KEY_SESSION_PORT"
"""Port used by the provider for the key session server."""

ENV_ARQIT_PY_CLIENT_CONFIG: str = "ARQIT_PY_CLIENT_CONFIG"
"""Path to the Arqit client (initiator) JSON configuration file."""

ENV_ARQIT_PY_SERVER_CONFIG: str = "ARQIT_PY_SERVER_CONFIG"
"""Path to the Arqit server (receiver) JSON configuration file."""

ENV_ARQIT_PY_TRANSPORT: str = "ARQIT_PY_TRANSPORT"
"""Arqit transport: "socket" (default) | "mqtt"."""

# Auth
ENV_AUTH_LOADER_BACKEND: str = "AUTH_LOADER_BACKEND"
"""API key loader backend: "static" | "file" | "postgres". Default: "static"."""

ENV_AUTH_KEYS_FILE: str = "AUTH_KEYS_FILE"
"""Path to the YAML/JSON API keys file (used by FileLoader)."""

ENV_AUTH_PG_DSN: str = "AUTH_PG_DSN"
"""asyncpg DSN for PostgresLoader (e.g. postgresql://user:pw@host/db)."""

ENV_AUTH_KEY_TTL: str = "AUTH_KEY_TTL"
"""TTL in seconds for KeyRecords in Redis. Default: 3600."""

ENV_AUTH_REFRESH_INTERVAL: str = "AUTH_REFRESH_INTERVAL"
"""Interval in seconds for the BackgroundRefresher. Default: 300."""


# ---------------------------------------------------------------------------
# STLS handshake endpoints
# ---------------------------------------------------------------------------

HANDSHAKE_PATH: str = "/stls/handshake"
"""Path of the STLS handshake endpoint, identical on stls-a2a and stls-mcp."""

HANDSHAKE_PQC_PATH: str = "/stls/pqc/handshake"
"""
Path of the PQC handshake endpoint (X25519 + ML-KEM-768).
Active when KEY_PROVIDER_NAME=pqc on stls-a2a and stls-mcp.
Extended wire format: includes the client's pubkey (PQCHandshakeRequest)
and the server's pubkey + ML-KEM ciphertext (PQCHandshakeResponse).
"""
