from .stls_a2a_transport import SecureA2ATransport, A2AMessage
from .stls_a2a_adapters_model import AgentResponse
from .stls_a2a_integration import (
    SecureA2ATransportAdapter,
    create_secure_a2a_endpoint,
    create_pqc_a2a_transport,
    AuthConfig)

# Providers — now live in stls_core (shared with stls-mcp).
# Only "base" and "arqit_py" are supported (JVM/simple removed).
from stls_core import (
    KeyProviderFactory,
    KeyProvider,
    KeyProviderClient,
    KeyProviderServer,
    KeySessionProvider,
    KeySessionProviderFactory,
)

from .stls_a2a_card_model import AgentCard
from .stls_a2a_utils import (
    to_a2a,
    start_task_store_cleanup,
    extract_a2a_text,
    extract_a2a_result,
    extract_a2a_artifacts)
from .stls_a2a_client import A2AClient

# Auth — now lives in stls_core (shared with stls-mcp).
from stls_core import (
    ApiKeyManager,
    ApiKeyManagerGroup,
    default_audit_hook,
    AuditEntry,
    generate_api_key,
    hash_key,
    RedisKeyCache,
)
from stls_core.auth.stls_core_auth_loaders import (
    ApiKeyLoader,
    StaticLoader,
    FileLoader,
    PostgresLoader,
    create_loader,
)

from .stls_a2a_agent_registry import AgentRegistry, make_agent_id
from .stls_a2a_remote import ContextualRemoteA2aAgent
from .stls_a2a_secure_remote import (
    SecureSessionManager,
    STLSHttpxTransport,
    ContextualSecureRemoteA2aAgent,
)