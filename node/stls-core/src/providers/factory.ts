/**
 * providers/factory.ts
 * ---------------------
 * Port of python/stls_core_providers_factory.py — singleton factories for
 * selecting and instantiating the key provider.
 *
 *   KeyProviderFactory        — selects a KeyProvider from a name
 *   KeySessionProviderFactory — creates, starts, and returns a ready
 *                               KeySessionProvider in one call
 *
 * Supported providers: "base" (zero dependencies, fixed key), "pqc"
 * (hybrid X25519 + ML-KEM-768, no external service). "arqit_py" has no
 * native Node SDK — it is only reachable through the Python sidecar
 * (KeyProviderArqitSidecar), so it's handled directly in
 * KeySessionProviderFactory rather than by KeyProviderFactory: unlike
 * Python, selecting it here also needs to resolve which of the two
 * SEPARATE sidecar sockets to use (SERVER and CLIENT must be two
 * different sidecar processes, never the same one — see
 * python/stls-core/sidecar/ARQIT_PEERING_NOTES.md), which depends on the
 * session type being requested.
 *
 * Simpler than Python's factory by construction, not just by omission:
 * Python's KeySessionProviderFactory.get_session_provider() has to
 * resolve a possibly-async start() with its own asyncio.run() internally,
 * which only works when called from sync top-level code (never from
 * inside an already-running event loop — a real, documented pitfall
 * there). Node has no such restriction: getSessionProvider() is just an
 * async function that awaits start() directly (awaiting a non-Promise
 * value is a no-op), so it can be called from anywhere, including from
 * inside another async function.
 *
 * Deliberately NOT ported as a literal singleton-instance class (Python's
 * `KeyProviderFactory()` / `KeySessionProviderFactory()` pattern): this
 * package's own SessionCacheFactory.build() already established the
 * Node-idiomatic convention of a plain static method with no per-instance
 * state, since none of these factories hold any state of their own.
 */

import {
  KeyProvider,
  KeyProviderBase,
  KeySessionProvider,
  KeySessionProviderType,
} from "./base.js";
import { KeyProviderPQC } from "./pqc.js";
import { KeyProviderArqitSidecar } from "./arqit-sidecar.js";

// ---------------------------------------------------------------------------
// KeyProviderFactory
// ---------------------------------------------------------------------------

export class KeyProviderFactory {
  /**
   * Returns the KeyProvider matching `name`.
   *
   * @param name "base" | "pqc" — NOT "arqit_py": use
   *   KeySessionProviderFactory.getSessionProvider() for that, which
   *   resolves the role-specific sidecar socket this provider needs and
   *   isn't a concern of this lower-level factory.
   */
  static getProvider(name: string): KeyProvider {
    const normalized = name.trim().toLowerCase();

    if (normalized === "base") {
      return new KeyProviderBase();
    }

    if (normalized === "pqc") {
      return new KeyProviderPQC();
    }

    if (normalized === "arqit_py") {
      throw new Error(
        "KeyProviderFactory: 'arqit_py' has no single KeyProvider instance here — " +
          "it needs a role-specific sidecar socket (SERVER and CLIENT must use two " +
          "different sidecar processes). Use KeySessionProviderFactory.getSessionProvider() " +
          "instead, or construct KeyProviderArqitSidecar directly.",
      );
    }

    throw new Error(`KeyProviderFactory: unsupported provider: '${name}'. Valid values here: 'base', 'pqc'.`);
  }
}

// ---------------------------------------------------------------------------
// KeySessionProviderFactory
// ---------------------------------------------------------------------------

export interface GetSessionProviderOptions {
  sessionType: KeySessionProviderType;
  /** Provider name override. If omitted, reads KEY_PROVIDER_NAME from the environment. */
  name?: string;
  /** CLIENT only; ignored by the current providers (base/pqc/arqit_py all resolve their own endpoint). Kept for interface parity with Python. */
  host?: string;
  /** Ignored by the current providers. Kept for interface parity with Python. */
  port?: number;
  /**
   * arqit_py only: path to the sidecar socket for THIS role. If omitted,
   * reads ARQIT_SIDECAR_SOCKET_SERVER or ARQIT_SIDECAR_SOCKET_CLIENT
   * depending on `sessionType` — deliberately NOT the single, role-agnostic
   * ARQIT_SIDECAR_SOCKET that KeyProviderArqitSidecar itself falls back to,
   * because that fallback would let SERVER and CLIENT accidentally resolve
   * to the same sidecar process (the one arqit_py scenario that reliably
   * fails against the real Arqit cloud — see ARQIT_PEERING_NOTES.md).
   */
  socketPath?: string;
}

export class KeySessionProviderFactory {
  /**
   * Creates, starts, and returns a ready KeySessionProvider.
   *
   * @throws Error if no provider name is resolved (neither `options.name`
   *   nor KEY_PROVIDER_NAME is set), or if 'arqit_py' is selected without
   *   a resolvable role-specific socket path.
   */
  static async getSessionProvider(options: GetSessionProviderOptions): Promise<KeySessionProvider> {
    const providerName = (options.name ?? process.env.KEY_PROVIDER_NAME ?? "").trim().toLowerCase();
    if (!providerName) {
      throw new Error(
        "KeySessionProviderFactory: KEY_PROVIDER_NAME is not set. " +
          "Set the environment variable or pass options.name explicitly.",
      );
    }

    const keyProvider =
      providerName === "arqit_py"
        ? new KeyProviderArqitSidecar({
            socketPath: options.socketPath ?? resolveArqitSidecarSocketEnv(options.sessionType),
            providerName: "arqit_py",
          })
        : KeyProviderFactory.getProvider(providerName);

    if (options.sessionType === KeySessionProviderType.SERVER) {
      const server = keyProvider.createProviderServer(options.port ?? 0, {});
      await server.start();
      return server.createProviderSession();
    }

    const client = keyProvider.createProviderClient(options.host ?? "", options.port ?? 0, {});
    await client.start();
    return client.createProviderSession();
  }
}

function resolveArqitSidecarSocketEnv(sessionType: KeySessionProviderType): string {
  const envVar = sessionType === KeySessionProviderType.SERVER ? "ARQIT_SIDECAR_SOCKET_SERVER" : "ARQIT_SIDECAR_SOCKET_CLIENT";
  const socketPath = process.env[envVar];
  if (!socketPath) {
    throw new Error(
      `KeySessionProviderFactory: 'arqit_py' requires ${envVar} (or options.socketPath) — ` +
        "the SERVER and CLIENT roles must run as two DIFFERENT sidecar processes, never the " +
        "same one (see python/stls-core/sidecar/ARQIT_PEERING_NOTES.md).",
    );
  }
  return socketPath;
}
