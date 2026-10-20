import os
from typing import Literal,Annotated
from dotenv import load_dotenv
from pydantic import BaseModel, Field,ConfigDict
from pydantic.alias_generators import to_camel

import logging
logger = logging.getLogger(__name__)
load_dotenv()

KEY_PROVIDER_NAME= os.getenv("KEY_PROVIDER_NAME")

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
  
class SupportedInterfaces(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
    json_rpc: bool = False   # serialized as jsonRpc
    grpc: bool = False
    rest: bool = False

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
    security_scheme: SecuritySchemeType = Field(
        default_factory=lambda: HTTPSecurityScheme(scheme="bearer")
    )
    supported_interfaces: SupportedInterfaces = Field(default_factory=lambda: SupportedInterfaces(json_rpc=True))
    extensions: list[AgentExtension] = Field(default_factory=list)    

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True, by_alias=True)