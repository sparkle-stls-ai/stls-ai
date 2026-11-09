import { Request, Response, NextFunction } from 'express';
import dns from 'node:dns/promises';
import net from 'node:net';
import { config } from '../config';

/**
 * Blocks a route unless the server runs with VITE_MODE=debug, independently
 * of AUTH_MODE (requireAuth is a no-op under AUTH_MODE=none).
 */
export function requireDebugMode(_req: Request, res: Response, next: NextFunction): void {
  if (config.MODE !== 'debug') {
    res.status(403).json({ error: 'Not available outside debug mode' });
    return;
  }
  next();
}

function isLinkLocal(ip: string): boolean {
  if (net.isIPv4(ip)) return ip.startsWith('169.254.');
  if (net.isIPv6(ip)) {
    const lower = ip.toLowerCase();
    if (lower.startsWith('::ffff:')) {
      // IPv4-mapped: URL() normalizes dotted form to hex (::ffff:a9fe:a9fe)
      const rest = lower.slice(7);
      if (rest.includes('.')) return isLinkLocal(rest);
      return parseInt(rest.split(':')[0], 16) === 0xa9fe; // 169.254.x.x
    }
    return /^fe[89ab]/.test(lower); // fe80::/10
  }
  return false;
}

/**
 * Validates a debug "direct" target URL: http/https only, and the hostname must
 * not resolve to the link-local range (cloud metadata, 169.254.0.0/16).
 * Loopback/private addresses stay allowed: debug mode targets local agents.
 * Throws an Error with a user-facing message when rejected.
 */
export async function assertSafeDebugUrl(rawUrl: string): Promise<void> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error('Invalid URL');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('Only http/https URLs are allowed');
  }
  const host = parsed.hostname.replace(/^\[|\]$/g, '');
  const addrs = net.isIP(host)
    ? [host]
    : (await dns.lookup(host, { all: true })).map((a) => a.address);
  if (addrs.some(isLinkLocal)) {
    throw new Error('Target address is not allowed');
  }
}
