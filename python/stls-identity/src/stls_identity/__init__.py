"""
stls_identity
-------------
DID + Verifiable Credential identity layer for the STLS AI ecosystem.

Exports the public API of every submodule, same pattern as stls-core's
root package: consumers import from here
(``from stls_identity import IdentityManager``), not from the
individual submodules directly
(``from stls_identity.stls_id_issuer import IdentityManager``).
"""

from .stls_id_models import (
    CredentialProof,
    CredentialStatus,
    CredentialSubject,
    DIDDocument,
    KeyStorageTier,
    NetworkAuthorityCredential,
    OperationalCredential,
    OperationalCredentialSubject,
    StatusListCredential,
    StatusPurpose,
    VerificationMethod,
    VerifiableCredential,
    PresentationProof,
    VerifiablePresentation,
    TrustAnchorEntry,
)
from .stls_id_did import (
    bootstrap_signing_key,
    build_did_document,
    did_to_resolution_url,
    domain_and_path_to_did,
)
from .stls_id_keystore import (
    IdentityKeystoreBase,
    IdentityKeystoreFactory,
    InMemoryIdentityKeystore,
    RedisIdentityKeystore,
)
from .stls_id_status import MRI_SECONDS, StatusListManager
from .stls_id_issuer import DEFAULT_VALIDITY, IdentityManager
from .stls_id_delegation import DEFAULT_OPERATIONAL_VALIDITY, DelegationManager
from .stls_id_presentation import build_verifiable_presentation
from .stls_id_verifier import CredentialVerificationError, Sentinel
from .stls_id_postgres_store import (
    DDL,
    DIDDocumentStoreBase,
    PostgresDIDDocumentStore,
    PostgresStatusListCacheStore,
    StatusListCacheStoreBase,
)
from .stls_id_bridge import create_identity_bridge_router
from .stls_id_trust_registry_store import (
    TrustAnchorRegistryStoreBase,
    PostgresTrustAnchorRegistryStore,
    build_trust_anchor_entry,
)
from .stls_id_trust_registry_client import (
    TRUST_ANCHOR_CACHE_TTL_SECONDS,
    TrustAnchorRegistryClient,
    TrustAnchorRegistryStaleError,
)
from .stls_id_trust_registry_bridge import create_trust_registry_router

__version__ = "0.1.0"

__all__ = [
    # models
    "CredentialProof",
    "CredentialStatus",
    "CredentialSubject",
    "DIDDocument",
    "KeyStorageTier",
    "NetworkAuthorityCredential",
    "OperationalCredential",
    "OperationalCredentialSubject",
    "StatusListCredential",
    "StatusPurpose",
    "VerificationMethod",
    "VerifiableCredential",
    "PresentationProof",
    "VerifiablePresentation",
    "TrustAnchorEntry",
    # did
    "bootstrap_signing_key",
    "build_did_document",
    "did_to_resolution_url",
    "domain_and_path_to_did",
    # keystore
    "IdentityKeystoreBase",
    "IdentityKeystoreFactory",
    "InMemoryIdentityKeystore",
    "RedisIdentityKeystore",
    # status
    "MRI_SECONDS",
    "StatusListManager",
    # issuer
    "DEFAULT_VALIDITY",
    "IdentityManager",
    # delegation
    "DEFAULT_OPERATIONAL_VALIDITY",
    "DelegationManager",
    # presentation
    "build_verifiable_presentation",
    # verifier
    "CredentialVerificationError",
    "Sentinel",
    # postgres_store
    "DDL",
    "DIDDocumentStoreBase",
    "PostgresDIDDocumentStore",
    "PostgresStatusListCacheStore",
    "StatusListCacheStoreBase",
    # bridge
    "create_identity_bridge_router",
    # trust_registry_store
    "TrustAnchorRegistryStoreBase",
    "PostgresTrustAnchorRegistryStore",
    "build_trust_anchor_entry",
    # trust_registry_client
    "TRUST_ANCHOR_CACHE_TTL_SECONDS",
    "TrustAnchorRegistryClient",
    "TrustAnchorRegistryStaleError",
    # trust_registry_bridge
    "create_trust_registry_router",
]