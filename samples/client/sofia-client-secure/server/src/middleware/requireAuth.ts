import { Request, Response, NextFunction } from 'express';
import { config } from '../config';

/**
 * Guards protected routes.
 * - AUTH_MODE=none : always passes through (no session needed)
 * - AUTH_MODE=app|user : verifies session exists and is not inactive
 *
 * On inactivity expiry:
 *   - app  → 401 { error: 'Session expired', authMode: 'app' }
 *   - user → 401 { error: 'Session expired', authMode: 'user' }  (frontend redirects to /login)
 */
export function requireAuth(req: Request, res: Response, next: NextFunction): void {
  if (config.AUTH_MODE === 'none') {
    next();
    return;
  }

  const sess = req.session;

  if (!sess.userId) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  // Inactivity check
  const now = Date.now();
  if (sess.lastActivity && now - sess.lastActivity > config.SESSION_TIMEOUT_MS) {
    req.session.destroy(() => {/* ignore */});
    res.status(401).json({ error: 'Session expired', authMode: sess.authMode ?? config.AUTH_MODE });
    return;
  }

  // Refresh lastActivity on each request
  sess.lastActivity = now;
  next();
}
