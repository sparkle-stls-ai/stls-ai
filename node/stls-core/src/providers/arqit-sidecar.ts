/**
 * providers/arqit-sidecar.ts
 * --------------------------
 * Node client for the Python sidecar that exposes the Arqit SKA-Platform
 * provider (python/stls-core/sidecar/arqit_sidecar.py) via JSON-RPC over a
 * Unix domain socket — Arqit does not publish a Node SDK, so peering stays
 * on the Python side and Node talks to that process over local IPC.
 *
 * One socket connection = one STLS session, the same "Option A" pattern
 * used by the Python arqit_py provider (a dedicated KeyProviderClient/
 * Server per session).
 *
 * Validated against both provider_name="base" (RPC mechanics, smoke
 * tests) and real provider_name="arqit_py" peering against the live
 * Arqit SKA-Platform™ cloud — see tests/interop/arqit-sidecar-real.live.ts
 * and tests/interop/provider-factory-arqit-real.live.ts.
 */

import { createConnection, type Socket } from "node:net";
import {
  KeyProvider,
  KeyProviderClient,
  KeyProviderServer,
  KeySessionProvider,
  KeySessionProviderType,
} from "./base.js";

interface RpcRequest {
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

interface RpcResponse {
  id: number | null;
  result?: unknown;
  error?: { message: string; code: number };
}

/** Newline-delimited JSON-RPC connection to a Python sidecar over a Unix domain socket. */
class SidecarRpcConnection {
  private readonly socket: Socket;
  private nextId = 1;
  private buffer = "";
  private readonly pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private connectPromise: Promise<void>;

  constructor(socketPath: string) {
    this.socket = createConnection({ path: socketPath });
    this.connectPromise = new Promise((resolve, reject) => {
      this.socket.once("connect", () => resolve());
      this.socket.once("error", reject);
    });
    this.socket.setEncoding("utf-8");
    this.socket.on("data", (chunk: string) => this.onData(chunk));
    this.socket.on("error", (err) => this.rejectAllPending(err));
    this.socket.on("close", () => this.rejectAllPending(new Error("sidecar connection closed")));
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let newlineIndex: number;
    while ((newlineIndex = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newlineIndex);
      this.buffer = this.buffer.slice(newlineIndex + 1);
      if (line.trim() === "") continue;

      const response = JSON.parse(line) as RpcResponse;
      if (response.id === null) continue;
      const waiter = this.pending.get(response.id);
      if (!waiter) continue;
      this.pending.delete(response.id);

      if (response.error) {
        waiter.reject(new Error(`[sidecar] ${response.error.message} (code=${response.error.code})`));
      } else {
        waiter.resolve(response.result);
      }
    }
  }

  private rejectAllPending(err: Error): void {
    for (const [, waiter] of this.pending) {
      waiter.reject(err);
    }
    this.pending.clear();
  }

  async call(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    await this.connectPromise;
    const id = this.nextId++;
    const request: RpcRequest = { id, method, params };

    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.write(`${JSON.stringify(request)}\n`);
    });
  }

  close(): void {
    this.socket.end();
  }
}

// ---------------------------------------------------------------------------
// KeySessionProvider over the sidecar
// ---------------------------------------------------------------------------

export class ArqitSidecarKeySessionProvider implements KeySessionProvider {
  constructor(
    private readonly rpc: SidecarRpcConnection,
    private readonly sessionType: KeySessionProviderType,
  ) {}

  getSessionType(): KeySessionProviderType {
    return this.sessionType;
  }

  async getSessionKeyIdentifier(hintKid?: string): Promise<string> {
    const params = hintKid ? { kid: hintKid } : undefined;
    const result = (await this.rpc.call("get_session_key_identifier", params)) as { kid: string };
    return result.kid;
  }

  setSessionKeyIdentifier(sessionKeyIdentifier: string): void {
    void this.rpc.call("set_session_key_identifier", { kid: sessionKeyIdentifier });
  }

  async getSessionKeyData(sessionKeyIdentifier: string): Promise<Buffer> {
    const result = (await this.rpc.call("get_session_key_data", { kid: sessionKeyIdentifier })) as {
      session_key_b64: string;
    };
    return Buffer.from(result.session_key_b64, "base64");
  }

  getPeerIdentity(): string | null {
    // NB: the sidecar resolves this value asynchronously (RPC), but the
    // KeySessionProvider interface requires it to be synchronous — a known
    // limitation of this scaffold. Use getPeerIdentityAsync() when the
    // real value returned by the remote provider is needed.
    return null;
  }

  async getPeerIdentityAsync(): Promise<string | null> {
    const result = (await this.rpc.call("get_peer_identity")) as { peer_identity: string | null };
    return result.peer_identity;
  }
}

// ---------------------------------------------------------------------------
// KeyProviderClient / KeyProviderServer over the sidecar
// ---------------------------------------------------------------------------

export interface ArqitSidecarOptions {
  /** Path to the Python sidecar's Unix domain socket. Default: env ARQIT_SIDECAR_SOCKET. */
  socketPath?: string;
  /** Python provider to instantiate on the sidecar: "arqit_py" (production) | "base" (test). */
  providerName?: string;
  /**
   * arqit_py only: overrides ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG
   * on the Python sidecar side for THIS connection only — lets several
   * connections to the same sidecar process use different device
   * configs. Sent to the sidecar as "config_path" in the "start" RPC
   * params; mirrors stls-core's own
   * KeySessionProviderFactory.get_session_provider(options=...) on the
   * Python side. Omitted (undefined) falls back to the sidecar's env
   * var exactly as before this option existed.
   */
  configPath?: string;
}

/**
 * Per-call options["configPath"] (the generic KeyProvider ABC slot,
 * mirroring Python's options: dict[str, Any]) overrides the
 * sidecar-level ArqitSidecarOptions.configPath — takes priority when
 * present, same precedence as Python's KeyProviderArqitPy.
 */
function mergeConfigPath(base: ArqitSidecarOptions, perCall: Record<string, unknown>): ArqitSidecarOptions {
  const configPath = typeof perCall.configPath === "string" ? perCall.configPath : base.configPath;
  return { ...base, configPath };
}

function resolveSocketPath(options: ArqitSidecarOptions): string {
  const path = options.socketPath ?? process.env.ARQIT_SIDECAR_SOCKET;
  if (!path) {
    throw new Error(
      "ArqitSidecar: socketPath not set. Pass { socketPath } or set ARQIT_SIDECAR_SOCKET.",
    );
  }
  return path;
}

export class KeyProviderClientArqitSidecar implements KeyProviderClient {
  private rpc: SidecarRpcConnection | null = null;
  private session: ArqitSidecarKeySessionProvider | null = null;

  constructor(private readonly options: ArqitSidecarOptions) {}

  async start(): Promise<void> {
    this.rpc = new SidecarRpcConnection(resolveSocketPath(this.options));
    await this.rpc.call("start", {
      session_type: "CLIENT",
      provider_name: this.options.providerName ?? process.env.KEY_PROVIDER_NAME ?? "base",
      config_path: this.options.configPath,
    });
    this.session = new ArqitSidecarKeySessionProvider(this.rpc, KeySessionProviderType.CLIENT);
  }

  createProviderSession(): KeySessionProvider {
    if (this.session === null) throw new Error("[KeyProviderClientArqitSidecar] start() has not been called yet");
    return this.session;
  }

  async stop(): Promise<void> {
    if (this.rpc !== null) {
      await this.rpc.call("stop");
      this.rpc.close();
    }
  }
}

export class KeyProviderServerArqitSidecar implements KeyProviderServer {
  private rpc: SidecarRpcConnection | null = null;
  private session: ArqitSidecarKeySessionProvider | null = null;

  constructor(private readonly options: ArqitSidecarOptions) {}

  async start(): Promise<void> {
    this.rpc = new SidecarRpcConnection(resolveSocketPath(this.options));
    await this.rpc.call("start", {
      session_type: "SERVER",
      provider_name: this.options.providerName ?? process.env.KEY_PROVIDER_NAME ?? "base",
      config_path: this.options.configPath,
    });
    this.session = new ArqitSidecarKeySessionProvider(this.rpc, KeySessionProviderType.SERVER);
  }

  createProviderSession(): KeySessionProvider {
    if (this.session === null) throw new Error("[KeyProviderServerArqitSidecar] start() has not been called yet");
    return this.session;
  }

  async stop(): Promise<void> {
    if (this.rpc !== null) {
      await this.rpc.call("stop");
      this.rpc.close();
    }
  }
}

/** Selection: an explicit socketPath or env ARQIT_SIDECAR_SOCKET. */
export class KeyProviderArqitSidecar implements KeyProvider {
  constructor(private readonly options: ArqitSidecarOptions = {}) {}

  getName(): string {
    return "arqit_sidecar";
  }

  createProviderClient(
    _host: string,
    _port: number,
    options: Record<string, unknown>,
  ): KeyProviderClient {
    return new KeyProviderClientArqitSidecar(mergeConfigPath(this.options, options));
  }

  createProviderServer(_port: number, options: Record<string, unknown>): KeyProviderServer {
    return new KeyProviderServerArqitSidecar(mergeConfigPath(this.options, options));
  }
}
