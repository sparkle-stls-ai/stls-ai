/**
 * constants.ts
 * ------------
 * Shared constants for stls-mcp: endpoint paths, MCP-specific HTTP headers,
 * and the STLS extension discovery key. Generic STLS headers (X-STLS-*)
 * stay in stls-core; only what's specific to the MCP protocol lives here.
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_constants.py.
 */

/**
 * Path of the standard MCP endpoint, generated and fully managed by the
 * official SDK. No STLS modification is applied on this path: it must
 * behave indistinguishably from a "pure" MCP server to any spec-compliant
 * client (Claude Desktop, claude.ai, MCP Inspector, etc.).
 */
export const MCP_PLAIN_PATH = "/mcp";

/**
 * Path of the STLS-encrypted MCP endpoint. Same session semantics and the
 * same tool/resource/prompt surface as MCP_PLAIN_PATH — the STLS handler
 * decrypts inbound, re-dispatches to the exact same request logic used by
 * the plain path, and encrypts outbound.
 */
export const MCP_STLS_PATH = "/mcp-stls";

/**
 * Native MCP Streamable HTTP session header. Case-insensitive on the wire.
 * Read-only for audit/logging on MCP_STLS_PATH — never used for the
 * cryptographic binding, which goes through HEADER_SESSION_ID in
 * stls-core, a distinct value from the MCP session.
 */
export const HEADER_MCP_SESSION_ID = "mcp-session-id";

/** Negotiated MCP protocol version header, required by the spec on requests after initialize(). Passed through as-is. */
export const HEADER_MCP_PROTOCOL_VERSION = "mcp-protocol-version";

/**
 * Key under which the STLS extension is declared in the initialize()
 * response, under capabilities.experimental. A client unaware of the
 * extension simply ignores it (it's under "experimental", by MCP design)
 * and keeps using MCP_PLAIN_PATH.
 */
export const STLS_EXTENSION_KEY = "stls";

/** Unique extension URI, consistent with the one already used in stls-a2a. */
export const STLS_EXTENSION_URI = "https://stls-ai.com/extensions/mcp-secure-transport/v1";

/**
 * Same numeric value and meaning as stls_a2a's ERR_SESSION_EXPIRED: raised
 * by encryptedHandler (middleware.ts) with status 401 when the session
 * cache has no entry for the given session_id — idle-TTL or absolute
 * max-age expiry (issue #6). Lets a client distinguish this from any
 * other 401 (e.g. an auth-layer API-key rejection), so only this code
 * justifies an automatic retry after re-handshake — see transport.ts's
 * rehandshake option.
 */
export const ERR_SESSION_EXPIRED = -32004;
