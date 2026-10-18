"""stls_core auth package — API key management."""

from .stls_core_auth_models import (
    AuthConfig,
    AuthResult,
    AuditEntry,
    DEFAULT_KEY_TTL_SECONDS,
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    KeyRecord,
    LoaderBackend,
    REDIS_INDEX_SET,
    REDIS_KEY_PREFIX,
    REDIS_REVOKED_SET,
    ValidationResult,
    constant_time_equal,
    generate_api_key,
    hash_key,
)
from .stls_core_auth_loaders import ApiKeyLoader, create_loader
from .stls_core_auth_store import RedisKeyCache
from .stls_core_auth_manager import (
    ApiKeyManager,
    ApiKeyManagerGroup,
    BackgroundRefresher,
    default_audit_hook,
)

__all__ = [
    # models
    "AuthConfig",
    "AuthResult",
    "AuditEntry",
    "DEFAULT_KEY_TTL_SECONDS",
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "KeyRecord",
    "LoaderBackend",
    "REDIS_INDEX_SET",
    "REDIS_KEY_PREFIX",
    "REDIS_REVOKED_SET",
    "ValidationResult",
    "constant_time_equal",
    "generate_api_key",
    "hash_key",
    # loaders
    "ApiKeyLoader",
    "create_loader",
    # store
    "RedisKeyCache",
    # manager
    "ApiKeyManager",
    "ApiKeyManagerGroup",
    "BackgroundRefresher",
    "default_audit_hook",
]