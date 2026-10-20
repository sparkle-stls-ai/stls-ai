// ---------------------------------------------------------------------------
// Bearer session token — fallback for when the `sofia.sid` cookie can't
// survive a cross-site context (e.g. embedded in a third-party iframe such
// as the Salesforce/BCAME Customer Portal, where browsers refuse to persist
// or send third-party session cookies).
//
// Kept in memory for the life of the tab, and mirrored to sessionStorage so
// it survives an iframe reload (sessionStorage is per-tab and per-origin,
// same as this module already is — nothing new is shared with the embedder).
// Never sent anywhere except this app's own `/api/*` via the Authorization
// header (see apiFetch.ts).
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'sofia.sessionToken';

let token: string | null = null;

try {
  token = window.sessionStorage.getItem(STORAGE_KEY);
} catch {
  // sessionStorage unavailable (privacy mode, blocked storage, etc.) —
  // fall back to in-memory only for this page load.
}

export function getSessionToken(): string | null {
  return token;
}

export function setSessionToken(value: string | null): void {
  token = value;
  try {
    if (value) window.sessionStorage.setItem(STORAGE_KEY, value);
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore — in-memory value is still set for this page load
  }
}
