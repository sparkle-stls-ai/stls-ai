"""
stls_core
---------
Protocol-agnostic cryptographic core for STLS.

Exports the public API of all submodules.
Child packages (stls-a2a, stls-mcp) import from here.
"""

from .stls_core_constants import (
    ENV_ARQIT_PY_CLIENT_CONFIG,
    ENV_ARQIT_PY_SERVER_CONFIG,
    ENV_ARQIT_PY_TRANSPORT,
    ENV_AUTH_KEY_TTL,
    ENV_AUTH_KEYS_FILE,
    ENV_AUTH_LOADER_BACKEND,
    ENV_AUTH_PG_DSN,
    ENV_AUTH_REFRESH_INTERVAL,
    ENV_KEY_PROVIDER_NAME,
    ENV_KEY_SESSION_PORT,
    HANDSHAKE_PATH,
    HANDSHAKE_PQC_PATH,
    HEADER_API_KEY,
    HEADER_AUTHORIZATION,
    HEADER_KID,
    HEADER_PROFILE,
    HEADER_SESSION_ID,
    HEADER_USER_ID,
    HKDF_INFO_AES,
    HKDF_INFO_HMAC,
    STLS_VERSION,
)
from .stls_core_crypto import StlsEnvelope, StlsTransport
from .stls_core_session import (
    SessionCache,
    SessionCacheBase,
    SessionCacheFactory,
    InMemorySessionCache,
    RedisSessionCache,
    StlsSession,
)
from .stls_core_handshake import (
    HandshakeError,
    HandshakeRequest,
    HandshakeResponse,
    perform_handshake,
    verify_handshake_proof,
)
from .stls_core_handshake_pqc import (
    PQCHandshakeError,
    PQCHandshakeRequest,
    PQCHandshakeResponse,
    derive_session_key_client,
    perform_pqc_handshake,
    verify_pqc_handshake_proof,
)
from .stls_core_client_handshake import (
    StlsHandshakeFailed,
    perform_client_handshake,
    perform_pqc_client_handshake,
)
from .stls_core_session_establish import establish_session, resolve_transport
from .providers import (
    BlockableResult,
    KeyProvider,
    KeyProviderBase,
    KeyProviderClient,
    KeyProviderClientBase,
    KeyProviderFactory,
    KeyProviderServer,
    KeyProviderServerBase,
    KeySessionProvider,
    KeySessionProviderBase,
    KeySessionProviderFactory,
    # PQC
    KeyProviderPQC,
    KeyProviderClientPQC,
    KeyProviderServerPQC,
    KeySessionProviderPQC,
    PQCKeyStore,
    derive_hybrid_key,
    mlkem_decap,
    mlkem_encap,
    mlkem_generate_keypair,
    x25519_exchange,
    x25519_generate_keypair,
)
from .auth import (
    ApiKeyLoader,
    ApiKeyManager,
    ApiKeyManagerGroup,
    AuthResult,
    AuditEntry,
    BackgroundRefresher,
    KeyRecord,
    LoaderBackend,
    RedisKeyCache,
    ValidationResult,
    create_loader,
    default_audit_hook,
    generate_api_key,
    hash_key,
)

__version__ = "0.1.8"

__all__ = [
    # constants
    "STLS_VERSION",
    "HANDSHAKE_PATH",
    "HANDSHAKE_PQC_PATH",
    "HEADER_SESSION_ID",
    "HEADER_KID",
    "HEADER_API_KEY",
    "HEADER_AUTHORIZATION",
    "HEADER_USER_ID",
    "HEADER_PROFILE",
    "HKDF_INFO_AES",
    "HKDF_INFO_HMAC",
    "ENV_KEY_PROVIDER_NAME",
    "ENV_KEY_SESSION_PORT",
    "ENV_ARQIT_PY_CLIENT_CONFIG",
    "ENV_ARQIT_PY_SERVER_CONFIG",
    "ENV_ARQIT_PY_TRANSPORT",
    "ENV_AUTH_LOADER_BACKEND",
    "ENV_AUTH_KEYS_FILE",
    "ENV_AUTH_PG_DSN",
    "ENV_AUTH_KEY_TTL",
    "ENV_AUTH_REFRESH_INTERVAL",
    # crypto
    "StlsEnvelope",
    "StlsTransport",
    # session
    "StlsSession",
    "SessionCache",
    "SessionCacheBase",
    "SessionCacheFactory",
    "InMemorySessionCache",
    "RedisSessionCache",
    # standard handshake
    "HandshakeRequest",
    "HandshakeResponse",
    "HandshakeError",
    "perform_handshake",
    "verify_handshake_proof",
    # PQC handshake
    "PQCHandshakeRequest",
    "PQCHandshakeResponse",
    "PQCHandshakeError",
    "perform_pqc_handshake",
    "derive_session_key_client",
    "verify_pqc_handshake_proof",
    # client handshake
    "StlsHandshakeFailed",
    "perform_client_handshake",
    "perform_pqc_client_handshake",
    # session establishment (issue #5)
    "establish_session",
    "resolve_transport",
    # providers
    "BlockableResult",
    "KeyProvider",
    "KeyProviderBase",
    "KeyProviderClient",
    "KeyProviderClientBase",
    "KeyProviderServer",
    "KeyProviderServerBase",
    "KeySessionProvider",
    "KeySessionProviderBase",
    "KeyProviderFactory",
    "KeySessionProviderFactory",
    # PQC providers
    "KeyProviderPQC",
    "KeyProviderClientPQC",
    "KeyProviderServerPQC",
    "KeySessionProviderPQC",
    "PQCKeyStore",
    "derive_hybrid_key",
    "mlkem_decap",
    "mlkem_encap",
    "mlkem_generate_keypair",
    "x25519_exchange",
    "x25519_generate_keypair",
    # auth
    "AuthResult",
    "AuditEntry",
    "KeyRecord",
    "LoaderBackend",
    "ValidationResult",
    "ApiKeyLoader",
    "create_loader",
    "RedisKeyCache",
    "ApiKeyManager",
    "ApiKeyManagerGroup",
    "BackgroundRefresher",
    "default_audit_hook",
    "generate_api_key",
    "hash_key",
]
