import { Request, Response, NextFunction } from 'express';
import { config } from '../config';

/**
 * Minimal security headers relevant to running Sofia Chat inside a
 * third-party iframe (Salesforce/BCAME Customer Portal):
 *
 * - frame-ancestors restricts *who* may embed this app in an iframe.
 *   Configure via FRAME_ANCESTORS (space-separated origins):
 *     - unset/empty (default) → no restriction: any site may embed the app.
 *       Chosen as the default so new integrators work out of the box without
 *       a config change; tighten it (see below) once the set of embedding
 *       domains is known and stable.
 *     - set → ONLY the listed origins may embed the app (plus 'self').
 *   NOTE: leaving this unset removes the app's only defense against
 *   clickjacking (a malicious site iframing the app to hijack an
 *   authenticated user's session) — the sofia.sid cookie's SameSite=Lax
 *   used to provide that defense implicitly, but the Bearer-token fallback
 *   (bearerAuth.ts) that makes cross-site iframes work at all bypasses it.
 *   Set FRAME_ANCESTORS to the known integrator domain(s) as soon as
 *   possible; treat the open default as a temporary/dev convenience.
 * - Referrer-Policy: no-referrer avoids leaking the one-time app_token
 *   (present in the initial iframe URL's query string) to any resource the
 *   page subsequently loads.
 */
export function securityHeaders(_req: Request, res: Response, next: NextFunction): void {
  const ancestors = config.FRAME_ANCESTORS.trim();
  if (ancestors) {
    res.setHeader('Content-Security-Policy', `frame-ancestors 'self' ${ancestors}`);
  }
  // else: no Content-Security-Policy header at all → browsers apply no
  // framing restriction (equivalent to the historical no-X-Frame-Options
  // default), so any site can embed the app.
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  if (config.NODE_ENV === 'production') {
    // Ignored by browsers over plain http; no includeSubDomains on purpose.
    res.setHeader('Strict-Transport-Security', 'max-age=15552000');
  }
  next();
}
