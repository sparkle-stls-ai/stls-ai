/**
 * providers/session-resolver.ts
 * -------------------------------
 * TypeScript twin of python/stls_core/providers/stls_core_providers_session_resolver.py's
 * KeySessionResolver — generic, provider-agnostic resolution of
 * (kid, kdata) pairs for an arbitrary caller-defined targetKey,
 * optionally giving each target its own arqit_py device config
 * (configPath) instead of the single process-wide env var.
 *
 * Scope: resolves raw key material for one target, safely under
 * concurrent callers for the SAME targetKey (a per-targetKey async
 * lock, mirroring the Python lock dict). Does NOT cache a
 * higher-level object — callers keep caching their own derived object
 * (a toolset, an adapter), keyed the same way, calling resolve() only
 * on their own cache miss.
 *
 * NOT exported from the package's public API (providers/index.ts /
 * index.ts) — resolve() hands back raw key material, exactly what
 * issue #5 ("Restrict stls-core's public API surface") closes off.
 * This class is now an internal implementation detail of
 * providers/session-establish.ts's getTransport(), which owns the
 * per-targetKey resolver instances and never lets the raw kdata escape.
 */

import { KeySessionProviderFactory } from "./factory.js";
import { KeySessionProviderType } from "./base.js";

export interface KeySessionResolverOptions {
  providerName: string;
  /**
   * arqit_py only: {targetKey: configPath} map. A targetKey with no
   * entry here (or this option omitted) falls back to whatever the
   * sidecar's own env var resolves to — no behavior change for
   * existing callers.
   */
  targetConfigs?: Record<string, string>;
}

export interface ResolveOptions {
  host?: string;
  port?: number;
  /** arqit_py only: which sidecar socket to use for this call. */
  socketPath?: string;
  /**
   * arqit_py only: device config_path for THIS call, overriding whatever
   * this.options.targetConfigs[targetKey] would otherwise resolve to —
   * lets a stateless caller (e.g. getTransport()) supply a single
   * resolved path per call instead of pre-building a whole targetKey map.
   */
  configPath?: string;
}

export interface ResolvedKeyMaterial {
  kid: string;
  kdata: Buffer;
}

export class KeySessionResolver {
  private readonly locks = new Map<string, Promise<unknown>>();

  constructor(private readonly options: KeySessionResolverOptions) {}

  /**
   * Returns {kid, kdata} for targetKey, serialized per targetKey so
   * concurrent callers resolving the SAME target don't race each
   * other into duplicate provider instances.
   */
  async resolve(targetKey: string, opts: ResolveOptions = {}): Promise<ResolvedKeyMaterial> {
    const prior = this.locks.get(targetKey) ?? Promise.resolve();
    const result = prior.then(() => this.doResolve(targetKey, opts));
    // Store a settled-or-not promise so the NEXT caller for this key
    // waits on this one regardless of success/failure.
    this.locks.set(targetKey, result.catch(() => undefined));
    return result;
  }

  private async doResolve(targetKey: string, opts: ResolveOptions): Promise<ResolvedKeyMaterial> {
    const configPath = opts.configPath ?? this.options.targetConfigs?.[targetKey];
    const provider = await KeySessionProviderFactory.getSessionProvider({
      sessionType: KeySessionProviderType.CLIENT,
      name: this.options.providerName,
      host: opts.host,
      port: opts.port,
      socketPath: opts.socketPath,
      configPath,
    });
    const kid = await provider.getSessionKeyIdentifier();
    const kdata = await provider.getSessionKeyData(kid);
    return { kid, kdata };
  }
}
