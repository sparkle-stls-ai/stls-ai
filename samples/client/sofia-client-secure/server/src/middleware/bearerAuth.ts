import { Request, Response, NextFunction } from 'express';
import { sign } from 'cookie-signature';
import { config } from '../config';

/**
 * Bearer-token compatibility shim for the session cookie.
 *
 * Some environments (e.g. Sofia Chat embedded in a third-party iframe, such as
 * the Salesforce/BCAME Customer Portal) run in a cross-site context where
 * browsers refuse to persist or send the `sofia.sid` session cookie, even with
 * `credentials: 'include'`. To keep working there, the SPA may instead send
 * the session id it got from `/api/auth/exchange` as `Authorization: Bearer
 * <sessionToken>`.
 *
 * This middleware runs *before* `express-session` and, when a Bearer token is
 * present and no `sofia.sid` cookie was already sent, synthesizes the exact
 * signed cookie value express-session would have produced for that session id
 * and injects it into `req.headers.cookie`. From that point on,
 * express-session loads/saves the session from the same Postgres store exactly
 * as it does for a normal cookie-based request — no other code (requireAuth,
 * routes, session typings) needs to know the difference.
 *
 * The `sofia.sid` cookie set on the response for cookie-capable clients (plain
 * tabs, same-site embeds, `AUTH_MODE=user`) is completely unaffected.
 */
export function bearerAuth(req: Request, _res: Response, next: NextFunction): void {
  const header = req.headers.authorization;
  if (!header || !header.startsWith('Bearer ')) {
    next();
    return;
  }

  const existingCookies = req.headers.cookie ?? '';
  if (/(?:^|;\s*)sofia\.sid=/.test(existingCookies)) {
    // A real cookie was already sent (e.g. same-site tab) — prefer it, don't override.
    next();
    return;
  }

  const sessionToken = header.slice('Bearer '.length).trim();
  if (!sessionToken) {
    next();
    return;
  }

  const signed = 's:' + sign(sessionToken, config.SESSION_SECRET);
  const synthesizedCookie = `sofia.sid=${encodeURIComponent(signed)}`;
  req.headers.cookie = existingCookies ? `${existingCookies}; ${synthesizedCookie}` : synthesizedCookie;

  next();
}
