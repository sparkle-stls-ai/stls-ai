from typing import Literal,Annotated
from pydantic import BaseModel, Field,ConfigDict, model_validator
from pydantic.alias_generators import to_camel

import logging
logger = logging.getLogger(__name__)

# ── A2A Agent Card models (spec-compliant) ────────────────────────────────────
class AgentSkill(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    input_modes: list[str] = ["text"]
    output_modes: list[str] = ["text"]

# ── Structured Security Schemes (spec §4.5) ──────────────────────────────────

class APIKeySecurityScheme(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    type: Literal["apiKey"] = "apiKey"
    description: str | None = None
    in_: Literal["header", "query", "cookie"] = Field("header", alias="in")
    name: str = "X-API-Key"                    # name of the header/query field

class HTTPSecurityScheme(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    type: Literal["http"] = "http"
    description: str | None = None
    scheme: Literal["bearer", "basic", "digest"] = "bearer"
    bearer_format: str | None = None           # e.g. "JWT"

class OAuth2SecurityScheme(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    type: Literal["oauth2"] = "oauth2"
    description: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: dict[str, str] = Field(default_factory=dict)  # scope → description

class OpenIdConnectSecurityScheme(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    type: Literal["openIdConnect"] = "openIdConnect"
    description: str | None = None
    open_id_connect_url: str                   # REQUIRED by the spec

class StlsKeyExchangeSecurityScheme(BaseModel):
    """
    STLS extension for applied key exchange — covers both the
    quantum-safe case (arqit_py, pqc) and the fixed-key/dev case (base).

    Renamed from SparkleSKASecurityScheme (type "sparkle_ska"): that name
    was the branding of the Arqit Sparkle SKA product, but the PQC case
    (X25519 + ML-KEM-768) has no relation to Arqit — it was semantically
    wrong, not just a branding issue exposed publicly in the card.
    "sparkle_ska" no longer appears even in the wire format
    (breaking change for anyone already parsing securityScheme.type=="sparkle_ska",
    deliberate: the old value was inaccurate for half of the cases it
    represented).
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    type: Literal["stls_key_exchange"] = "stls_key_exchange"
    description: str | None = None
    key_provider: str | None = None            # "base" | "arqit_py" | "pqc"

# Union type for AgentCard
SecurityScheme = (
    APIKeySecurityScheme
    | HTTPSecurityScheme
    | OAuth2SecurityScheme
    | OpenIdConnectSecurityScheme
    | StlsKeyExchangeSecurityScheme
)

# The only "type" values the A2A spec (0.3 and 1.0 alike) actually
# recognizes for SecurityScheme — a closed set, no custom/extension
# variant in either version. See AgentCard._populate_spec_fields().
_OFFICIAL_SECURITY_SCHEME_TYPES = frozenset({"apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"})


class SecureTransport(BaseModel):
    enabled: bool = True
    encryption: str = "AES-256-GCM"
    
class AgentCapabilities(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,   # allows snake_case to still be used internally
    )
    streaming: bool = False
    push_notifications: bool = False          # serialized as pushNotifications
    state_transition_history: bool = False    # serialized as stateTransitionHistory
  
class AgentInterface(BaseModel):
    """
    A2A 1.0 AgentInterface (spec §4.4.6) — declares a URL + transport
    binding this agent is reachable at. AgentCard.supportedInterfaces is
    a REQUIRED array of these; the first entry is the preferred one.

    Replaces the old SupportedInterfaces shape (a {jsonRpc, grpc, rest}
    boolean flag object) that happened to reuse the spec's field name
    (supportedInterfaces) with an unrelated, non-conformant type — any
    strict 1.0 AgentCard validator would reject that shape outright.
    Nothing in this codebase read those booleans (grep-verified before
    removing it), so there was no real consumer to stay compatible with.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    url: str
    protocol_binding: str          # open string; spec's officially-supported values: JSONRPC, GRPC, HTTP+JSON
    protocol_version: str = "1.0"
    tenant: str | None = None

class AgentExtension(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
    uri: str                        # unique URI identifying the extension
    required: bool = False          # whether the client MUST support it
    params: dict | None = None      # optional extension parameters
    description: str | None = None

SecuritySchemeType = Annotated[
    APIKeySecurityScheme
    | HTTPSecurityScheme
    | OAuth2SecurityScheme
    | OpenIdConnectSecurityScheme
    | StlsKeyExchangeSecurityScheme,
    Field(discriminator="type")
]

class AgentCard(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    ) 
    name: str
    description: str
    url: str | None = None           # plain endpoint /a2a (standard A2A)
    secure_url: str | None = None    # STLS encrypted endpoint /secure-a2a
    version: str = "1.0.0"
    provider: dict | None = None
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])   # → defaultInputModes
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])  # → defaultOutputModes
    skills: list[AgentSkill] = Field(default_factory=list)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    # Kept for backward compatibility (read by stls_a2a_client.py,
    # rendered by the sofia-client-secure debug UI): the single scheme
    # this agent actually uses. security_schemes (below) is the new,
    # spec-shaped field — both are populated from the same value, see
    # _populate_spec_fields().
    security_scheme: SecuritySchemeType = Field(
        default_factory=lambda: HTTPSecurityScheme(scheme="bearer")
    )
    # A2A 1.0 field (spec §4.4.1, field 8): a MAP of named security
    # schemes, vs. security_scheme's single object. Auto-populated from
    # security_scheme (keyed by its `type`) when not given explicitly,
    # so every existing _card_from_*() builder gets this for free.
    #
    # Known partial gap: the spec's proto wraps each scheme in a oneof
    # (e.g. {"apiKeySecurityScheme": {...}}); the values here stay in
    # the flatter {"type": "apiKey", ...} shape SecuritySchemeType
    # already used for security_scheme, rather than that nested form.
    # Revisit if a strict 1.0 validator flags it — no real consumer in
    # this codebase depends on the exact inner shape today.
    security_schemes: dict[str, SecuritySchemeType] = Field(default_factory=dict)
    # A2A 1.0 REQUIRED field (spec §4.4.1, field 3): an ordered array of
    # AgentInterface{url, protocolBinding, protocolVersion}. Auto-populated
    # from `url` (above) when not given explicitly, so every existing
    # _card_from_*() builder gets this for free — see AgentInterface's
    # docstring for why this replaces the old SupportedInterfaces shape.
    supported_interfaces: list[AgentInterface] = Field(default_factory=list)
    extensions: list[AgentExtension] = Field(default_factory=list)

    @model_validator(mode="after")
    def _populate_spec_fields(self) -> "AgentCard":
        """
        Derives the A2A 1.0-shaped fields from their pre-existing,
        still-supported equivalents when the caller didn't set them
        explicitly — additive only, so no _card_from_*() builder needs
        to change to get spec-conformant output.
        """
        if not self.supported_interfaces and self.url:
            self.supported_interfaces = [
                AgentInterface(url=self.url, protocol_binding="JSONRPC", protocol_version="1.0")
            ]
        # securitySchemes' values are a CLOSED union in both the 0.3 and
        # 1.0 spec (apiKey/http/oauth2/openIdConnect/mutualTLS — no
        # "custom" variant in either). Our own StlsKeyExchangeSecurityScheme
        # (type "stls_key_exchange") has no conformant representation in
        # that union, and emitting it anyway isn't a soft violation: the
        # official a2a-sdk 0.3.26 client's AgentCard model validates this
        # strictly and HARD-FAILS card resolution entirely — confirmed
        # against a real client, not assumed (issue #14). securitySchemes
        # is optional in both spec versions, so only populate it for
        # schemes that are actually one of the 5 official types; for our
        # own extension type, omitting the field is the spec-legal choice,
        # unlike emitting something that breaks every real client.
        if not self.security_schemes and self.security_scheme and self.security_scheme.type in _OFFICIAL_SECURITY_SCHEME_TYPES:
            self.security_schemes = {self.security_scheme.type: self.security_scheme}
        return self

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True, by_alias=True)