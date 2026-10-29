import { Request, Response } from 'express';

/**
 * In-memory limiter for FAILED credential checks (login / app-login).
 *
 * Only failures are counted and a success clears the counter, so normal use
 * is never affected. Two buckets per request:
 *   - ip + user_id : slows brute force against a single account
 *   - ip           : slows password spraying across many accounts
 *
 * State is per process (a multi-instance deploy gets the limit per instance),
 * which is enough to make online guessing impractical without a new dependency.
 */
const WINDOW_MS = 15 * 60 * 1000;
const MAX_PER_USER = 10;
const MAX_PER_IP = 50;

interface Bucket { count: number; resetAt: number }
const buckets = new Map<string, Bucket>();

setInterval(() => {
  const now = Date.now();
  for (const [k, b] of buckets) if (b.resetAt <= now) buckets.delete(k);
}, 60_000).unref();

function keys(req: Request, userId: unknown): [string, string] {
  const ip = req.ip ?? 'unknown';
  return [`u:${ip}|${String(userId ?? '').slice(0, 200)}`, `i:${ip}`];
}

function live(key: string): Bucket | undefined {
  const b = buckets.get(key);
  if (b && b.resetAt <= Date.now()) {
    buckets.delete(key);
    return undefined;
  }
  return b;
}

/** Sends a 429 and returns true when the caller is over the limit. */
export function rejectIfLimited(req: Request, res: Response, userId: unknown): boolean {
  const [userKey, ipKey] = keys(req, userId);
  const u = live(userKey);
  const i = live(ipKey);
  const limited = (u && u.count >= MAX_PER_USER) || (i && i.count >= MAX_PER_IP);
  if (!limited) return false;

  const resetAt = Math.max(u?.resetAt ?? 0, i?.resetAt ?? 0);
  res.setHeader('Retry-After', String(Math.max(1, Math.ceil((resetAt - Date.now()) / 1000))));
  res.status(429).json({ error: 'Too many failed attempts, try again later' });
  return true;
}

export function recordFailure(req: Request, userId: unknown): void {
  for (const key of keys(req, userId)) {
    const b = live(key);
    if (b) b.count++;
    else buckets.set(key, { count: 1, resetAt: Date.now() + WINDOW_MS });
  }
}

export function recordSuccess(req: Request, userId: unknown): void {
  buckets.delete(keys(req, userId)[0]);
}
