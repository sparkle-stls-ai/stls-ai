/**
 * providers/session-establish.ts
 * --------------------------------
 * getTransport() — the public, non-leaking replacement for the old
 * two-step "KeySessionResolver.resolve() then feed kdata into
 * performClientHandshake()" dance (issue #5, "Restrict stls-core's
 * public API surface"). Resolves the session key AND performs the
 * handshake (or, for pqc, the KEM handshake) in one call; the raw key
 * never leaves this module.
 *
 * KeySessionResolver itself stays internal (not exported from
 * providers/index.ts / index.ts) — this module is now its only
 * consumer, holding one resolver instance per providerName so the
 * existing per-targetKey lock (duplicate-handshake protection) keeps
 * working exactly as before, without requiring callers to construct
 * and hold their own KeySessionResolver anymore.
 */

import {
  PerformClientHandshakeOptions,
  StlsClientSession,
  performClientHandshake,
  performPqcClientHandshake,
} from "../client-handshake.js";
import { KeySessionResolver } from "./session-resolver.js";
import type { FetchLike } from "../client-handshake.js";

export interface GetTransportOptions {
  /** "base" | "pqc" | "arqit_py" */
  providerName: string;
  /** Handshake target; also the resolver/lock cache key (mirrors every existing call site, which always used the same value for both). */
  baseUrl: string;
  clientId: string;
  /** arqit_py only: device config override for THIS call. */
  configPath?: string;
  /** arqit_py only: which sidecar socket to use. */
  socketPath?: string;
  host?: string;
  port?: number;
  handshakePath?: string;
  fetch?: FetchLike;
}

export type GetTransportResult = StlsClientSession;

const resolvers = new Map<string, KeySessionResolver>();

function getResolver(providerName: string): KeySessionResolver {
  let resolver = resolvers.get(providerName);
  if (resolver === undefined) {
    resolver = new KeySessionResolver({ providerName });
    resolvers.set(providerName, resolver);
  }
  return resolver;
}

/**
 * Resolves the session key for `options.baseUrl` under `options.providerName`
 * and performs the STLS handshake, returning a ready StlsTransport —
 * never the raw key itself. For "pqc", resolution and the KEM handshake
 * are the same step (no pre-shared key, nothing to resolve).
 */
export async function getTransport(options: GetTransportOptions): Promise<GetTransportResult> {
  const providerName = options.providerName.trim().toLowerCase();

  if (providerName === "pqc") {
    return performPqcClientHandshake(options.baseUrl, {
      clientId: options.clientId,
      handshakePath: options.handshakePath,
      fetch: options.fetch,
    });
  }

  const resolver = getResolver(providerName);
  const { kid: resolvedKid, kdata } = await resolver.resolve(options.baseUrl, {
    host: options.host,
    port: options.port,
    socketPath: options.socketPath,
    configPath: options.configPath,
  });

  const handshakeOptions: PerformClientHandshakeOptions = {
    clientId: options.clientId,
    kid: resolvedKid,
    handshakePath: options.handshakePath,
    fetch: options.fetch,
  };
  return performClientHandshake(options.baseUrl, kdata, handshakeOptions);
}
