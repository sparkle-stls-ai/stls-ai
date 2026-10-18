/**
 * providers/base.ts
 * -----------------
 * Port of python/stls-core/src/stls_core/providers/stls_core_providers_base.py.
 *
 * Only the KeySessionProvider interface and the Base provider (fixed key,
 * dev/test) are ported in this iteration. arqit_py and pqc have their own
 * dedicated files: arqit_py requires the proprietary Arqit SDK (no Node
 * library available — exposed to Node via a sidecar, see
 * providers/arqit-sidecar.ts), pqc is implemented natively with
 * @noble/post-quantum, see providers/pqc.ts.
 */

export enum KeySessionProviderType {
  CLIENT = "CLIENT",
  SERVER = "SERVER",
}

/**
 * Interface for a cryptographic session provider.
 * Used by performHandshake() and SessionCache to retrieve the session key
 * associated with a key identifier.
 */
export interface KeySessionProvider {
  getSessionType(): KeySessionProviderType;
  /** May be asynchronous: on remote providers (e.g. Arqit via the sidecar) it waits for negotiation. */
  getSessionKeyIdentifier(): Promise<string>;
  /** Explicitly sets the kid (used for session-reuse scenarios). */
  setSessionKeyIdentifier(sessionKeyIdentifier: string): void;
  getSessionKeyData(sessionKeyIdentifier: string): Promise<Buffer>;
  /** Peer identity (IP, device_id, etc.) or null. */
  getPeerIdentity(): string | null;
}

export interface KeyProviderClient {
  start(): Promise<void> | void;
  createProviderSession(): KeySessionProvider;
  stop(): Promise<void> | void;
}

export interface KeyProviderServer {
  start(): Promise<void>;
  createProviderSession(): KeySessionProvider;
  stop(): Promise<void>;
}

export interface KeyProvider {
  getName(): string;
  createProviderClient(host: string, port: number, options: Record<string, unknown>): KeyProviderClient;
  createProviderServer(port: number, options: Record<string, unknown>): KeyProviderServer;
}

// ---------------------------------------------------------------------------
// Base provider — fixed key, zero dependencies
// ---------------------------------------------------------------------------

const BASE_KEY_ID = "base-session-key-identifier";
const BASE_KEY_DATA = Buffer.from("0123456789abcdef", "utf-8"); // 16 bytes — enough for HKDF-Expand

/**
 * KeySessionProvider with a hard-coded fixed key. Used for development,
 * testing and CI. The key is known to both sides with no negotiation.
 */
export class KeySessionProviderBase implements KeySessionProvider {
  private kid: string = BASE_KEY_ID;

  constructor(private readonly sessionType: KeySessionProviderType) {}

  getSessionType(): KeySessionProviderType {
    return this.sessionType;
  }

  async getSessionKeyIdentifier(): Promise<string> {
    return this.kid;
  }

  setSessionKeyIdentifier(sessionKeyIdentifier: string): void {
    this.kid = sessionKeyIdentifier;
  }

  async getSessionKeyData(_sessionKeyIdentifier: string): Promise<Buffer> {
    return BASE_KEY_DATA;
  }

  getPeerIdentity(): string | null {
    return "base-peer";
  }
}

export class KeyProviderClientBase implements KeyProviderClient {
  start(): void {}

  createProviderSession(): KeySessionProvider {
    return new KeySessionProviderBase(KeySessionProviderType.CLIENT);
  }

  stop(): void {}
}

export class KeyProviderServerBase implements KeyProviderServer {
  async start(): Promise<void> {}

  createProviderSession(): KeySessionProvider {
    return new KeySessionProviderBase(KeySessionProviderType.SERVER);
  }

  async stop(): Promise<void> {}
}

/** Selection: KEY_PROVIDER_NAME=base */
export class KeyProviderBase implements KeyProvider {
  getName(): string {
    return "base";
  }

  createProviderClient(
    _host: string,
    _port: number,
    _options: Record<string, unknown>,
  ): KeyProviderClient {
    return new KeyProviderClientBase();
  }

  createProviderServer(_port: number, _options: Record<string, unknown>): KeyProviderServer {
    return new KeyProviderServerBase();
  }
}
