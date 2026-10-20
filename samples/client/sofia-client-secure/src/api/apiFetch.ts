import { getSessionToken } from './sessionToken';

// ---------------------------------------------------------------------------
// fetch() wrapper used for every call to this app's own /api/*.
//
// Always sends credentials:'include' (unchanged — same-site/standalone use
// keeps working exactly as before via the `sofia.sid` cookie). When a Bearer
// session token is available (set after /api/auth/exchange), it's added as
// an Authorization header too, so the call also works in contexts where the
// cookie didn't survive (cross-site iframe embeds). The backend accepts
// either — see server/src/middleware/bearerAuth.ts.
// ---------------------------------------------------------------------------
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const token = getSessionToken();
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);

  return fetch(input, { ...init, credentials: 'include', headers });
}
