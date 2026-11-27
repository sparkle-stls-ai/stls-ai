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
    PRESENTATION_HEADER,
    StatusListCredential,
    StatusPurpose,
    VerificationMethod,
    VerifiableCredential,
    PresentationProof,
    VerifiablePresentation,
    TrustAnchorEntry,
)
from .stls_id_did import (
    agent_id_from_verification_method_id,
    bootstrap_signing_key,
    build_did_document,
    current_verification_method,
    decode_multibase_ed25519_public_key,
    did_to_resolution_url,
    domain_and_path_to_did,
    make_did_document_holder_key_resolver,
    resolve_holder_key_at,
    rotate_agent_signing_key,
    rotate_signing_key,
)
from .stls_id_keystore import (
    IdentityKeystoreBase,
    IdentityKeystoreFactory,
    InMemoryIdentityKeystore,
    RedisIdentityKeystore,
)
from .stls_id_credential_cache import (
    IdentityCredentialCacheBase,
    IdentityCredentialCacheFactory,
    InMemoryIdentityCredentialCache,
    InMemoryOperationalCredentialCache,
    OperationalCredentialCacheBase,
    OperationalCredentialCacheFactory,
    RedisIdentityCredentialCache,
    RedisOperationalCredentialCache,
)
from .stls_id_status import (
    MRI_SECONDS,
    StatusListManager,
    fetch_remote_credential_status,
    fetch_status_bit,
)
from .stls_id_issuer import DEFAULT_VALIDITY, IdentityManager
from .stls_id_delegation import DEFAULT_OPERATIONAL_VALIDITY, DelegationManager, load_delegation_manager
from .stls_id_presentation import (
    IdentityPresenter,
    attach_identity_presentation_header,
    build_verifiable_presentation,
    identity_challenge_handler_from_env,
    load_delegation_presenter,
    load_identity_presenter,
)
from .stls_id_verifier import CredentialVerificationError, Sentinel
from .stls_id_request_verifier import (
    PresentationVerificationResult,
    RequestPresentationVerifier,
    StatusResolver,
)
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

__version__ = "0.1.7"

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
    "PRESENTATION_HEADER",
    "StatusListCredential",
    "StatusPurpose",
    "VerificationMethod",
    "VerifiableCredential",
    "PresentationProof",
    "VerifiablePresentation",
    "TrustAnchorEntry",
    # did
    "agent_id_from_verification_method_id",
    "bootstrap_signing_key",
    "build_did_document",
    "current_verification_method",
    "decode_multibase_ed25519_public_key",
    "did_to_resolution_url",
    "domain_and_path_to_did",
    "make_did_document_holder_key_resolver",
    "resolve_holder_key_at",
    "rotate_agent_signing_key",
    "rotate_signing_key",
    # keystore
    "IdentityKeystoreBase",
    "IdentityKeystoreFactory",
    "InMemoryIdentityKeystore",
    "RedisIdentityKeystore",
    # credential_cache
    "IdentityCredentialCacheBase",
    "IdentityCredentialCacheFactory",
    "InMemoryIdentityCredentialCache",
    "RedisIdentityCredentialCache",
    "OperationalCredentialCacheBase",
    "OperationalCredentialCacheFactory",
    "InMemoryOperationalCredentialCache",
    "RedisOperationalCredentialCache",
    # status
    "MRI_SECONDS",
    "StatusListManager",
    "fetch_remote_credential_status",
    "fetch_status_bit",
    # issuer
    "DEFAULT_VALIDITY",
    "IdentityManager",
    # delegation
    "DEFAULT_OPERATIONAL_VALIDITY",
    "DelegationManager",
    "load_delegation_manager",
    # presentation
    "IdentityPresenter",
    "attach_identity_presentation_header",
    "build_verifiable_presentation",
    "identity_challenge_handler_from_env",
    "load_delegation_presenter",
    "load_identity_presenter",
    # verifier
    "CredentialVerificationError",
    "Sentinel",
    # a2a_request_verifier
    "PresentationVerificationResult",
    "RequestPresentationVerifier",
    "StatusResolver",
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