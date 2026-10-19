"""
vault_server.py
----------------
Example MCP server for stls-mcp: "Deep Sky Vault", an archive of
quantum-safe space stations. Covers all three MCP protocol primitives
— tools, resources, prompts — exposed both in plaintext on
{MCP_PLAIN_PATH} (default "/mcp", standard MCP, zero modifications) and
STLS-encrypted on {MCP_STLS_PATH} (default "/mcp-stls").

Both paths are configurable via .env (MCP_PLAIN_PATH, MCP_STLS_PATH) —
useful for exposing the whole server under a prefix, e.g. "/vault/mcp".

Optional API key protection: set AUTH_ENABLED=true in .env to activate
ApiKeyManager.from_env() (static, file, or Postgres depending on
AUTH_LOADER_BACKEND) on BOTH paths — see build_api_key_manager() below
for the full list of relevant env vars. Without AUTH_ENABLED=true,
unchanged behavior: open endpoints.

Used as a fixture for the E2E tests (test_e2e_full_protocol.py) and as
a standalone demo that can be launched directly:

    python tests/vault_server.py
    # then, in another terminal:
    python tests/vault_client.py

Theme: a fleet of orbital stations that only communicate over
quantum-safe channels (consistent with the real-world domain of the
STLS project, but playful enough to be a good teaching example).
"""

from __future__ import annotations

import logging
import math
import os
import random
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from stls_core import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider
from stls_mcp import create_secure_mcp_app

# Optional — must not break startup if stls_core[redis] isn't installed
# and AUTH_ENABLED isn't set anyway (see build_api_key_manager below).
try:
    from stls_core import ApiKeyManager
    _AUTH_MANAGER_AVAILABLE = True
except ImportError:
    ApiKeyManager = None  # type: ignore[assignment,misc]
    _AUTH_MANAGER_AVAILABLE = False

load_dotenv()

# Without explicit configuration, the Arqit peering INFO/DEBUG logs
# (device authentication, listener threads, bilocation/P2P negotiation)
# stay silent. INFO level by default; pass --debug for DEBUG.
_log_level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)

# ---------------------------------------------------------------------------
# In-memory fleet "database" — nothing persistent, it's a demo
# ---------------------------------------------------------------------------


@dataclass
class Station:
    id: str
    name: str
    sector: str
    crew: int
    orbit_altitude_km: float
    status: str
    last_transmission: str


FLEET: dict[str, Station] = {
    "kepler-9": Station(
        id="kepler-9",
        name="Kepler Outpost",
        sector="Sigma Draconis",
        crew=12,
        orbit_altitude_km=408.0,
        status="nominal",
        last_transmission="All quantum links green. Crew morale high.",
    ),
    "vega-relay": Station(
        id="vega-relay",
        name="Vega Relay Station",
        sector="Lyra Expanse",
        crew=4,
        orbit_altitude_km=850.0,
        status="nominal",
        last_transmission="Relay buffers nominal, no anomalies detected.",
    ),
    "halcyon": Station(
        id="halcyon",
        name="Halcyon Deep-Field Array",
        sector="Orion Spur",
        crew=7,
        orbit_altitude_km=1200.0,
        status="degraded",
        last_transmission="Solar flare interference on array 3, rerouting.",
    ),
}

# Toy shift cipher, ONLY for the decode_transmission tool demo — has no
# relation to STLS's real cryptography (AES-256-GCM), it's just a
# textual puzzle to make the tool interesting to call.
_CIPHER_SHIFT = 7


def _caesar_encode(text: str, shift: int = _CIPHER_SHIFT) -> str:
    out = []
    for ch in text:
        if ch.isalpha():
            base = ord("A") if ch.isupper() else ord("a")
            out.append(chr((ord(ch) - base + shift) % 26 + base))
        else:
            out.append(ch)
    return "".join(out)


def _caesar_decode(text: str, shift: int = _CIPHER_SHIFT) -> str:
    return _caesar_encode(text, -shift)


# "Incoming" encrypted transmissions — the decode_transmission tool decrypts them
INCOMING_TRANSMISSIONS: dict[str, str] = {
    "kepler-9": _caesar_encode("All quantum links green. Crew morale high."),
    "vega-relay": _caesar_encode("Relay buffers nominal, no anomalies detected."),
    "halcyon": _caesar_encode("Solar flare interference on array 3, rerouting."),
}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

# Hosts allowed by the MCP SDK's DNS-rebinding protection. Read from
# MCP_ALLOWED_HOSTS (CSV), falling back to local-only hosts if unset.
# For testing with Claude Desktop via a tunnel (cloudflared/ngrok), add
# the tunnel's domain, e.g.:
#   export MCP_ALLOWED_HOSTS="127.0.0.1,127.0.0.1:*,localhost,localhost:*,keno-mice-acceptable-theology.trycloudflare.com"
_default_hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
_allowed_hosts_env = os.getenv("MCP_ALLOWED_HOSTS", "")
ALLOWED_HOSTS = (
    [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
    if _allowed_hosts_env
    else _default_hosts
)

# ---------------------------------------------------------------------------
# Endpoint paths — configurable via .env
# ---------------------------------------------------------------------------
# MCP_PLAIN_PATH determines where FastMCP mounts the standard endpoint
# (passed to the constructor's streamable_http_path — the ONLY point
# that decides the actual path; create_secure_mcp_app(plain_path=...)
# below only makes the startup log message match it, it doesn't change
# the routing).
# MCP_STLS_PATH determines where create_secure_mcp_app mounts the
# encrypted channel.
# Example to expose everything under a "/vault" prefix:
#   MCP_PLAIN_PATH=/vault/mcp
#   MCP_STLS_PATH=/vault/mcp-stls
MCP_PLAIN_PATH = os.getenv("MCP_PLAIN_PATH", "/mcp")
MCP_STLS_PATH = os.getenv("MCP_STLS_PATH", "/mcp-stls")

mcp = FastMCP(
    "Deep Sky Vault",
    instructions=(
        "Archive of quantum-safe space stations of the orbital fleet. "
        "Use list_stations / get_station_status to inspect the fleet, "
        "decode_transmission to decrypt incoming transmissions, "
        "calculate_orbit to estimate a station's orbital period, "
        "and the vault://manifest resource for a machine-readable summary."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path=MCP_PLAIN_PATH,
    transport_security=TransportSecuritySettings(
        allowed_hosts=ALLOWED_HOSTS,
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def list_stations() -> str:
    """List all stations currently in the fleet, with their status."""
    lines = [f"{s.id}: {s.name} ({s.sector}) — status: {s.status}" for s in FLEET.values()]
    return "\n".join(lines)


@mcp.tool()
def get_station_status(station_id: str) -> str:
    """Get detailed status for a single station by its id (e.g. 'kepler-9')."""
    station = FLEET.get(station_id)
    if station is None:
        known = ", ".join(FLEET.keys())
        return f"Unknown station '{station_id}'. Known stations: {known}"
    return (
        f"{station.name} ({station.id})\n"
        f"Sector: {station.sector}\n"
        f"Crew: {station.crew}\n"
        f"Orbit altitude: {station.orbit_altitude_km} km\n"
        f"Status: {station.status}"
    )


@mcp.tool()
def decode_transmission(station_id: str) -> str:
    """
    Decode the latest incoming transmission from a station.

    Transmissions arrive shifted through a simple substitution cipher
    (a toy puzzle for this demo — unrelated to STLS's real AES-256-GCM
    transport encryption). Returns the decoded plaintext message.
    """
    ciphertext = INCOMING_TRANSMISSIONS.get(station_id)
    if ciphertext is None:
        known = ", ".join(INCOMING_TRANSMISSIONS.keys())
        return f"No transmission on file for '{station_id}'. Known stations: {known}"
    return _caesar_decode(ciphertext)


@mcp.tool()
def calculate_orbit(altitude_km: float) -> str:
    """
    Estimate the orbital period (in minutes) for a circular orbit at the
    given altitude above Earth's surface, using simplified Keplerian
    mechanics (mu_earth = 398600.4418 km^3/s^2, R_earth = 6371 km).
    """
    mu_earth = 398600.4418  # km^3/s^2
    r_earth = 6371.0  # km
    radius = r_earth + altitude_km
    period_seconds = 2 * math.pi * math.sqrt(radius**3 / mu_earth)
    period_minutes = period_seconds / 60.0
    return (
        f"At {altitude_km:.1f} km altitude (orbital radius {radius:.1f} km), "
        f"the estimated orbital period is {period_minutes:.2f} minutes "
        f"({period_minutes / 60:.2f} hours)."
    )


@mcp.tool()
def signal_strength_check(station_id: str) -> str:
    """
    Simulate a quantum-safe link signal strength check for a station.
    Returns a pseudo-random but deterministic-per-call strength reading,
    purely for demo/testing purposes (no real telemetry involved).
    """
    if station_id not in FLEET:
        known = ", ".join(FLEET.keys())
        return f"Unknown station '{station_id}'. Known stations: {known}"
    strength = random.randint(72, 99)
    quality = "excellent" if strength >= 90 else "good" if strength >= 80 else "fair"
    return f"{station_id}: quantum link signal strength {strength}% ({quality})"


# ── Resources ──────────────────────────────────────────────────────────────

@mcp.resource("vault://manifest")
def fleet_manifest() -> str:
    """Machine-readable manifest of the entire fleet (JSON)."""
    import json

    return json.dumps(
        {
            "fleet": "Deep Sky Vault",
            "station_count": len(FLEET),
            "stations": [
                {
                    "id": s.id,
                    "name": s.name,
                    "sector": s.sector,
                    "status": s.status,
                }
                for s in FLEET.values()
            ],
        },
        indent=2,
    )


@mcp.resource("vault://station/{station_id}")
def station_detail(station_id: str) -> str:
    """Detailed JSON record for a single station."""
    import json

    station = FLEET.get(station_id)
    if station is None:
        return json.dumps({"error": f"Unknown station '{station_id}'"})
    return json.dumps(
        {
            "id": station.id,
            "name": station.name,
            "sector": station.sector,
            "crew": station.crew,
            "orbit_altitude_km": station.orbit_altitude_km,
            "status": station.status,
            "last_transmission": station.last_transmission,
        },
        indent=2,
    )


# ── Prompts ────────────────────────────────────────────────────────────────

@mcp.prompt()
def mission_briefing(station_id: str, urgency: str = "routine") -> str:
    """Generate a mission briefing prompt for a given station and urgency level."""
    station = FLEET.get(station_id)
    station_name = station.name if station else station_id
    return (
        f"You are a fleet operations officer for Deep Sky Vault. Prepare a "
        f"{urgency} mission briefing for {station_name}. Cover: current "
        f"status, any anomalies in the latest transmission, and one "
        f"recommended action for the crew."
    )


@mcp.prompt()
def fleet_summary(focus: str = "status") -> str:
    """Generate a prompt asking for a fleet-wide summary, focused on a given aspect."""
    return (
        f"Summarize the current state of the Deep Sky Vault fleet, focusing "
        f"on {focus}. Use list_stations and get_station_status to gather "
        f"data before writing the summary."
    )


# ---------------------------------------------------------------------------
# API key auth — optional, explicit gate via AUTH_ENABLED
# ---------------------------------------------------------------------------
# ApiKeyManager.from_env() ALWAYS builds a working manager, even with
# zero configuration — with AUTH_LOADER_BACKEND unset the default is
# "static" with ZERO keys loaded, which means "deny everything", not
# "auth disabled". That's why the AUTH_ENABLED gate is ours (not an
# stls_core.auth env var): without an explicit AUTH_ENABLED=true, we
# fall back to this file's historical behavior — api_key_manager=None,
# open endpoints — instead of accidentally enabling a total traffic
# block.
#
# Env vars relevant when AUTH_ENABLED=true (read by ApiKeyManager.from_env()):
#   AUTH_LOADER_BACKEND   static | file | postgres   (default: static)
#   AUTH_KEYS_FILE        YAML/JSON path              (for file)
#   AUTH_PG_DSN           PostgreSQL DSN              (for postgres)
#   SF_API_KEYS_TABLE     table name                  (for postgres, default sf_api_keys)
#   REDIS_URL             Redis URL                    (default redis://localhost:6379)
#   AUTH_KM_USE_REDIS     enables Redis cache          (default true)
#   AUTH_KEY_TTL          Redis TTL in seconds         (default 3600)
#   AUTH_REFRESH_INTERVAL seconds between refreshes    (default 300)
#
# The lifecycle (start/stop) is managed automatically by
# create_secure_mcp_app's lifespan — an instance that is NOT yet started
# is passed here (see the comment in
# stls_mcp_server.create_secure_mcp_app for why this differs from the
# stls-a2a pattern).
def build_api_key_manager() -> "ApiKeyManager | None":
    auth_enabled = os.getenv("AUTH_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    if not auth_enabled:
        return None
    if not _AUTH_MANAGER_AVAILABLE:
        raise RuntimeError(
            "AUTH_ENABLED=true but stls_core.auth is not available "
            "(a dependency is missing — check the stls_core installation)."
        )
    return ApiKeyManager.from_env()


# ---------------------------------------------------------------------------
# App assembly — plain {MCP_PLAIN_PATH} + encrypted {MCP_STLS_PATH}, via stls-mcp
# ---------------------------------------------------------------------------

# Reads KEY_PROVIDER_NAME from the environment ("base" or "arqit_py").
# With arqit_py, KEY_SESSION_PORT determines the port the SERVER
# provider listens on for peering with the client (see
# ARQIT_PY_SERVER_CONFIG/ARQIT_PY_CLIENT_CONFIG in .env for the
# SKA-Platform™ configuration).
_key_session_port = int(os.getenv("KEY_SESSION_PORT", "8091"))
provider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER,
    port=_key_session_port,
)
app = create_secure_mcp_app(
    mcp, provider,
    plain_path=MCP_PLAIN_PATH,
    stls_path=MCP_STLS_PATH,
    api_key_manager=build_api_key_manager(),
)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("MCP_SERVER_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_SERVER_PORT", "8765"))
    _provider_name = os.getenv("KEY_PROVIDER_NAME", "base")
    _auth_enabled = os.getenv("AUTH_ENABLED", "false").strip().lower() in ("true", "1", "yes")

    print("Deep Sky Vault started:")
    print(f"  plain     -> http://{host}:{port}{MCP_PLAIN_PATH}")
    print(f"  stls      -> http://{host}:{port}{MCP_STLS_PATH}")
    print(f"  handshake -> http://{host}:{port}/stls/handshake")
    if _provider_name == "pqc":
        print(f"  pqc-hs    -> http://{host}:{port}/stls/pqc/handshake  [active]")
    print(f"  provider  -> {_provider_name}")
    print(f"  allowed_hosts -> {ALLOWED_HOSTS}")
    if _auth_enabled:
        print(f"  auth      -> ENABLED (AUTH_LOADER_BACKEND={os.getenv('AUTH_LOADER_BACKEND', 'static')}) — protects both plain and stls")
    else:
        print("  auth      -> disabled (AUTH_ENABLED not set) — open endpoints")
    uvicorn.run(app, host=host, port=port)