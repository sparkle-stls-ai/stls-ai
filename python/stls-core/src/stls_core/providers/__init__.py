"""stls_core providers package."""

from .stls_core_providers_base import (
    BlockableResult,
    KeyProvider,
    KeyProviderBase,
    KeyProviderClient,
    KeyProviderClientBase,
    KeyProviderServer,
    KeyProviderServerBase,
    KeySessionProvider,
    KeySessionProviderBase,
)
from .stls_core_providers_factory import (
    KeyProviderFactory,
    KeySessionProviderFactory,
)
# KeySessionResolver is intentionally NOT re-exported here — resolve()
# hands back raw key material, exactly what issue #5 ("Restrict
# stls-core's public API surface") closes off. establish_session()/
# resolve_transport() (stls_core_session_establish.py) are the public
# replacement; they import it directly from
# .stls_core_providers_session_resolver.

from .stls_core_providers_keystore import (
    ArqitKeyStoreBase,
    ArqitKeyStoreFactory,
    InMemoryArqitKeyStore,
    RedisArqitKeyStore,
)

from .stls_core_providers_pqc import (
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

__all__ = [
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
    "ArqitKeyStoreBase",
    "ArqitKeyStoreFactory",
    "InMemoryArqitKeyStore",
    "RedisArqitKeyStore",
    # PQC
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
]