/**
 * constants.ts
 * ------------
 * Shared constants, a 1:1 port of python/stls-core/src/stls_core/stls_core_constants.py.
 *
 * Any change to the values below must stay in sync with the Python
 * counterpart — these are STLS wire-format literals, not local
 * implementation details.
 */

// STLS protocol version, included in every encrypted envelope.
export const STLS_VERSION = "1.0";

// HTTP headers
export const HEADER_SESSION_ID = "X-STLS-Session-ID";
export const HEADER_KID = "X-STLS-Kid";
export const HEADER_API_KEY = "X-API-Key";
export const HEADER_AUTHORIZATION = "Authorization";
export const HEADER_USER_ID = "X-STLS-User-Id";
export const HEADER_PROFILE = "X-STLS-Profile";

// Encrypted envelope fields (StlsTransport.encrypt() -> JSON)
export const ENVELOPE_VERSION = "version";
export const ENVELOPE_SESSION_ID = "session_id";
export const ENVELOPE_IV = "iv";
export const ENVELOPE_CIPHERTEXT = "ciphertext";
export const ENVELOPE_TAG = "tag";
export const ENVELOPE_MAC = "mac";

// HKDF domain separators (expand-only, PRK = session_key)
export const HKDF_INFO_AES = Buffer.from("stls-aes-256-gcm", "utf-8");
export const HKDF_INFO_HMAC = Buffer.from("stls-hmac-sha256", "utf-8");

// Environment variables
export const ENV_KEY_PROVIDER_NAME = "KEY_PROVIDER_NAME";
export const ENV_KEY_SESSION_PORT = "KEY_SESSION_PORT";
export const ENV_ARQIT_PY_CLIENT_CONFIG = "ARQIT_PY_CLIENT_CONFIG";
export const ENV_ARQIT_PY_SERVER_CONFIG = "ARQIT_PY_SERVER_CONFIG";
export const ENV_ARQIT_PY_TRANSPORT = "ARQIT_PY_TRANSPORT";
export const ENV_AUTH_LOADER_BACKEND = "AUTH_LOADER_BACKEND";
export const ENV_AUTH_KEYS_FILE = "AUTH_KEYS_FILE";
export const ENV_AUTH_PG_DSN = "AUTH_PG_DSN";
export const ENV_AUTH_KEY_TTL = "AUTH_KEY_TTL";
export const ENV_AUTH_REFRESH_INTERVAL = "AUTH_REFRESH_INTERVAL";

// STLS handshake endpoints
export const HANDSHAKE_PATH = "/stls/handshake";
export const HANDSHAKE_PQC_PATH = "/stls/pqc/handshake";
