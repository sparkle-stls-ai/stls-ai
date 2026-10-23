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
  /**
   * May be asynchronous: on remote providers (e.g. Arqit via the
   * sidecar) it waits for negotiation.
   *
   * hintKid: optional kid the caller already knows (e.g. the one a
   * client resolved on its own side and sent along in its
   * HandshakeRequest). Implementations that serve a single,
   * unambiguous key (Base, PQC) ignore it. The Arqit sidecar provider
   * uses it to resolve that EXACT already-delivered key directly
   * instead of "whichever key arrived and hasn't been claimed yet" —
   * needed because, on a long-running SERVER instance serving
   * multiple concurrent handshakes, plain FIFO delivery order does
   * not necessarily match the order handshake requests arrive in
   * (same fix as stls_core_providers_arqit_py.py's own hint_kid, see
   * its docstring for the full explanation).
   */
  getSessionKeyIdentifier(hintKid?: string): Promise<string>;
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

  async getSessionKeyIdentifier(_hintKid?: string): Promise<string> {
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
