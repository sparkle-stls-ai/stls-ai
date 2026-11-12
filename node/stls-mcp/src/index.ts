/**
 * stls-mcp (Node.js)
 * ------------------
 * STLS extension of the Model Context Protocol: application-layer
 * symmetric encryption (AES-256-GCM + HMAC-SHA256 + HKDF-Expand) over a
 * second Streamable HTTP endpoint, alongside a 100% standard MCP endpoint
 * generated and managed entirely by the official SDK.
 *
 * Node.js port of python/stls-mcp — see README.md for the porting status
 * and the architectural differences deliberately made at port time (no
 * ASGI-scope-rewriting needed, fresh McpServer+transport per request).
 */

export * from "./constants.js";
export * from "./auth.js";
export { StlsMcpError, encryptedHandler, type EncryptedHandlerOptions } from "./middleware.js";
export { createSecureMcpApp, stlsCapabilitiesFragment, type CreateSecureMcpAppOptions } from "./server.js";
export { toNodeRequestListener } from "./http-adapter.js";
export * from "./transport.js";

export const STLS_MCP_NODE_VERSION = "0.1.4";
