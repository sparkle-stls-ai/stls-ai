import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import bcrypt from 'bcryptjs';
import { pool } from '../db';
import { config } from '../config';
import type { SfUser } from '../types';

const router = Router();

// ---------------------------------------------------------------------------
// POST /api/auth/app-login
// Called server-to-server by an external app.
//
// Body:
//   app_name        — identifies the app in sf_user (e.g. "crm")
//   user_id         — sf_user credential used for authentication only
//   password        — plaintext password
//   caller_user_id  — end-user of the calling app (displayed in header + agent metadata)
//   caller_profile  — that user's profile object (optional, passed to agents)
//
// Returns: { token }  — one-time token valid for 5 minutes
// ---------------------------------------------------------------------------
router.post('/app-login', async (req: Request, res: Response): Promise<void> => {
  if (config.AUTH_MODE !== 'app') {
    res.status(404).json({ error: 'Not available in this auth mode' });
    return;
  }

  const { app_name, user_id, password, caller_user_id, caller_profile } = req.body as {
    app_name?: string;
    user_id?: string;
    password?: string;
    caller_user_id?: string;
    caller_profile?: Record<string, unknown>;
  };

  if (!app_name || !user_id || !password || !caller_user_id) {
    res.status(400).json({ error: 'app_name, user_id, password and caller_user_id are required' });
    return;
  }

  try {
    const result = await pool.query<SfUser>(
      'SELECT pwd_hash FROM sf_user WHERE user_id = $1 AND app_name = $2 AND type = $3 AND active = TRUE',
      [user_id, app_name, 'app']
    );

    if (!result.rows.length || !(await bcrypt.compare(password, result.rows[0].pwd_hash))) {
      res.status(401).json({ error: 'Invalid credentials' });
      return;
    }

    // One-time token valid for 5 minutes
    const token = crypto.randomBytes(32).toString('hex');
    const expiresAt = new Date(Date.now() + 5 * 60 * 1000);

    await pool.query(
      `INSERT INTO sf_app_tokens (token, user_id, app_name, caller_user_id, profile, expires_at)
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [token, user_id, app_name, caller_user_id, caller_profile ? JSON.stringify(caller_profile) : null, expiresAt]
    );

    res.json({ token });
  } catch (err) {
    console.error('[auth] app-login error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// ---------------------------------------------------------------------------
// GET /api/auth/exchange?app_token=<token>
// Browser-side: exchanges a one-time app token for a session.
// Called by the SPA on initial load when ?app_token is in the URL.
//
// Returns `sessionToken` (the session id) alongside the usual Set-Cookie.
// Same-site contexts can just rely on the cookie as before; a cross-site
// iframe embed (where the browser refuses to persist that cookie) instead
// keeps `sessionToken` in memory/sessionStorage and sends it back as
// `Authorization: Bearer <sessionToken>` on every subsequent call — see
// server/src/middleware/bearerAuth.ts.
// ---------------------------------------------------------------------------
router.get('/exchange', async (req: Request, res: Response): Promise<void> => {
  if (config.AUTH_MODE !== 'app') {
    res.status(404).json({ error: 'Not available in this auth mode' });
    return;
  }

  const { app_token } = req.query as { app_token?: string };
  if (!app_token) {
    res.status(400).json({ error: 'app_token required' });
    return;
  }

  try {
    const result = await pool.query(
      `SELECT user_id, app_name, caller_user_id, profile
       FROM sf_app_tokens
       WHERE token = $1 AND used = FALSE AND expires_at > NOW()`,
      [app_token]
    );

    if (!result.rows.length) {
      res.status(401).json({ error: 'Invalid or expired token' });
      return;
    }

    const { user_id, app_name, caller_user_id, profile } = result.rows[0] as {
      user_id: string;
      app_name: string | null;
      caller_user_id: string;
      profile: Record<string, unknown> | null;
    };

    // Mark token as used (one-time)
    await pool.query('UPDATE sf_app_tokens SET used = TRUE WHERE token = $1', [app_token]);

    // Build session:
    //   userId        = caller_user_id  (displayed in header, passed to agents)
    //   appAuthUserId = user_id         (sf_user credential — used only for access control)
    //   appName       = app_name
    //   profile       = caller_profile
    req.session.userId        = caller_user_id;
    req.session.appAuthUserId = user_id;
    req.session.userType      = 'app';
    req.session.appName       = app_name;
    req.session.profile       = profile;
    req.session.lastActivity  = Date.now();
    req.session.authMode      = 'app';

    res.json({ ok: true, userId: caller_user_id, appName: app_name, sessionToken: req.sessionID });
  } catch (err) {
    console.error('[auth] exchange error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// ---------------------------------------------------------------------------
// POST /api/auth/login
// User login: validates user credentials, creates session.
// ---------------------------------------------------------------------------
router.post('/login', async (req: Request, res: Response): Promise<void> => {
  if (config.AUTH_MODE !== 'user') {
    res.status(404).json({ error: 'Not available in this auth mode' });
    return;
  }

  const { user_id, password } = req.body as { user_id?: string; password?: string };

  if (!user_id || !password) {
    res.status(400).json({ error: 'user_id and password are required' });
    return;
  }

  try {
    const result = await pool.query<SfUser>(
      `SELECT pwd_hash FROM sf_user
       WHERE user_id = $1 AND type = 'user' AND active = TRUE`,
      [user_id]
    );

    if (!result.rows.length || !(await bcrypt.compare(password, result.rows[0].pwd_hash))) {
      res.status(401).json({ error: 'Invalid credentials' });
      return;
    }

    req.session.userId        = user_id;
    req.session.appAuthUserId = null;
    req.session.userType      = 'user';
    req.session.appName       = null;
    req.session.profile       = null;
    req.session.lastActivity  = Date.now();
    req.session.authMode      = 'user';

    res.json({ ok: true, userId: user_id });
  } catch (err) {
    console.error('[auth] login error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// ---------------------------------------------------------------------------
// POST /api/auth/logout
// ---------------------------------------------------------------------------
router.post('/logout', (req: Request, res: Response): void => {
  req.session.destroy(() => {
    res.clearCookie('sofia.sid');
    res.json({ ok: true });
  });
});

// ---------------------------------------------------------------------------
// GET /api/auth/me
// Returns current session info (used by frontend to check auth state).
// ---------------------------------------------------------------------------
router.get('/me', (req: Request, res: Response): void => {
  // AUTH_MODE=none: no identity — return authenticated without user info
  // (ignore any stale session left over from a previous auth mode)
  if (config.AUTH_MODE === 'none') {
    res.json({ authenticated: true, userId: null, appName: null, authMode: 'none', mode: config.MODE });
    return;
  }

  if (!req.session.userId) {
    res.status(401).json({ authenticated: false, authMode: config.AUTH_MODE });
    return;
  }

  const sess = req.session;
  const now = Date.now();
  if (sess.lastActivity && now - sess.lastActivity > config.SESSION_TIMEOUT_MS) {
    req.session.destroy(() => {/* ignore */});
    res.status(401).json({ error: 'Session expired', authMode: sess.authMode });
    return;
  }

  sess.lastActivity = now;
  res.json({
    authenticated: true,
    userId:   sess.userId,       // caller_user_id (app) | login user_id (user)
    appName:  sess.appName ?? null,
    userType: sess.userType,
    authMode: config.AUTH_MODE,
    mode:     config.MODE,
  });
});

// ---------------------------------------------------------------------------
// GET /api/auth/config  (public — no auth required)
// Returns app mode/auth config so the frontend can adapt its UI.
// ---------------------------------------------------------------------------
router.get('/config', (_req: Request, res: Response): void => {
  res.json({ mode: config.MODE, authMode: config.AUTH_MODE });
});

export default router;
