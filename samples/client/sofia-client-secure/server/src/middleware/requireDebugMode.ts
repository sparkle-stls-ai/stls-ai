import { Request, Response, NextFunction } from 'express';
import dns from 'dns/promises';
import { isIP } from 'net';
import { config } from '../config';

/**
 * Gates the /api/proxy/direct/* routes, which let the caller pass an
 * arbitrary target URL instead of an agent name resolved from sf_agents_config.
 * requireAuth alone is not enough here: AUTH_MODE=none (the shipped default)
 * disables it entirely, which would leave these routes open to anyone who can
 * reach the Express port. VITE_MODE=debug is a separate, explicit opt-in that
 * must be set regardless of AUTH_MODE.
 */
export function requireDebugMode(req: Request, res: Response, next: NextFunction): void {
  if (config.MODE !== 'debug') {
    res.status(403).json({ error: 'Direct agent calls are only available in debug mode' });
    return;
  }
  next();
}

// Cloud metadata services (AWS/GCP/Azure/Alibaba/Oracle all use link-local
// 169.254.0.0/16, most commonly 169.254.169.254) have no legitimate reason
// to ever be a debug target — unlike loopback/private addresses, which are
// exactly what local agent development points at. Block only this range.
function isBlockedMetadataAddress(ip: string): boolean {
  if (isIP(ip) === 4) {
    const [a, b] = ip.split('.').map(Number);
    return a === 169 && b === 254;
  }
  if (isIP(ip) === 6) {
    const normalized = ip.toLowerCase();
    return normalized === '::ffff:169.254.0.0' || normalized.startsWith('fe80:');
  }
  return false;
}

/**
 * Validates a user-supplied debug target URL before it is forwarded to the
 * secure transport service. Rejects non-http(s) schemes and cloud metadata
 * addresses (directly, or via DNS — a hostname resolving to 169.254.x.x).
 * Throws with a message safe to return to the caller.
 */
export async function assertSafeDebugUrl(rawUrl: string): Promise<void> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error('Invalid target URL');
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('Only http/https target URLs are allowed');
  }

  const host = parsed.hostname;
  if (isIP(host)) {
    if (isBlockedMetadataAddress(host)) {
      throw new Error('Target address is not allowed');
    }
    return;
  }

  try {
    const { address } = await dns.lookup(host);
    if (isBlockedMetadataAddress(address)) {
      throw new Error('Target address is not allowed');
    }
  } catch (err) {
    if (err instanceof Error && err.message === 'Target address is not allowed') throw err;
    // DNS lookup failure: let the downstream fetch fail with its own error
    // rather than masking it here.
  }
}
