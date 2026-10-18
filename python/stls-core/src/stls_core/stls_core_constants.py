"""
stls_core_constants.py
----------------------
Costanti condivise tra tutti i package STLS (stls-core, stls-a2a, stls-mcp).

Nessuna dipendenza esterna — solo stdlib.
Importabile da qualsiasi modulo senza rischio di circular import.
"""

# ---------------------------------------------------------------------------
# Versione protocollo STLS
# ---------------------------------------------------------------------------

STLS_VERSION: str = "1.0"
"""Versione del protocollo STLS inclusa in ogni envelope cifrato."""


# ---------------------------------------------------------------------------
# Header HTTP
# ---------------------------------------------------------------------------

# Identificatori di sessione STLS
HEADER_SESSION_ID: str = "X-STLS-Session-ID"
"""Header che trasporta il session_id STLS su ogni richiesta cifrata."""

HEADER_KID: str = "X-STLS-Kid"
"""Header che trasporta il key identifier (kid) della sessione corrente."""

# Autenticazione
HEADER_API_KEY: str = "X-API-Key"
"""Header standard per API key authentication."""

HEADER_AUTHORIZATION: str = "Authorization"
"""Header HTTP standard per Bearer token."""

# Metadati contesto (usati da stls-a2a, dichiarati qui per condivisione)
HEADER_USER_ID: str = "X-STLS-User-Id"
"""Header opzionale per propagare lo user_id nel contesto dell'agente."""

HEADER_PROFILE: str = "X-STLS-Profile"
"""Header opzionale per propagare il profilo utente nel contesto dell'agente."""


# ---------------------------------------------------------------------------
# Campi envelope cifrato
# ---------------------------------------------------------------------------

ENVELOPE_VERSION: str = "version"
ENVELOPE_SESSION_ID: str = "session_id"
ENVELOPE_IV: str = "iv"
ENVELOPE_CIPHERTEXT: str = "ciphertext"
ENVELOPE_TAG: str = "tag"
ENVELOPE_MAC: str = "mac"
"""
Chiavi dell'envelope JSON prodotto da StlsTransport.encrypt().

Struttura:
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
"""Info label per derivazione della chiave AES via HKDF-Expand."""

HKDF_INFO_HMAC: bytes = b"stls-hmac-sha256"
"""Info label per derivazione della chiave HMAC via HKDF-Expand."""


# ---------------------------------------------------------------------------
# Variabili d'ambiente
# ---------------------------------------------------------------------------

ENV_KEY_PROVIDER_NAME: str = "KEY_PROVIDER_NAME"
"""
Nome del key provider attivo.
Valori supportati: "base" | "arqit_py" | "pqc"
Default: "base"
"""

ENV_KEY_SESSION_PORT: str = "KEY_SESSION_PORT"
"""Porta usata dal provider per il key session server."""

ENV_ARQIT_PY_CLIENT_CONFIG: str = "ARQIT_PY_CLIENT_CONFIG"
"""Path al file JSON di configurazione del client Arqit (initiator)."""

ENV_ARQIT_PY_SERVER_CONFIG: str = "ARQIT_PY_SERVER_CONFIG"
"""Path al file JSON di configurazione del server Arqit (receiver)."""

ENV_ARQIT_PY_TRANSPORT: str = "ARQIT_PY_TRANSPORT"
"""Transport Arqit: "socket" (default) | "mqtt"."""

# Auth
ENV_AUTH_LOADER_BACKEND: str = "AUTH_LOADER_BACKEND"
"""Backend loader API key: "static" | "file" | "postgres". Default: "static"."""

ENV_AUTH_KEYS_FILE: str = "AUTH_KEYS_FILE"
"""Path al file YAML/JSON delle API key (usato da FileLoader)."""

ENV_AUTH_PG_DSN: str = "AUTH_PG_DSN"
"""DSN asyncpg per PostgresLoader (es. postgresql://user:pw@host/db)."""

ENV_AUTH_KEY_TTL: str = "AUTH_KEY_TTL"
"""TTL in secondi per i KeyRecord in Redis. Default: 3600."""

ENV_AUTH_REFRESH_INTERVAL: str = "AUTH_REFRESH_INTERVAL"
"""Intervallo in secondi del BackgroundRefresher. Default: 300."""


# ---------------------------------------------------------------------------
# Endpoint STLS handshake
# ---------------------------------------------------------------------------

HANDSHAKE_PATH: str = "/stls/handshake"
"""Path dell'endpoint di handshake STLS, identico su stls-a2a e stls-mcp."""

HANDSHAKE_PQC_PATH: str = "/stls/pqc/handshake"
"""
Path dell'endpoint di handshake PQC (X25519 + ML-KEM-768).
Attivo quando KEY_PROVIDER_NAME=pqc in stls-a2a e stls-mcp.
Wire format esteso: include pubkey client (PQCHandshakeRequest)
e pubkey server + ciphertext ML-KEM (PQCHandshakeResponse).
"""