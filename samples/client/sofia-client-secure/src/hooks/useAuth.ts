import { useState, useEffect, useCallback } from 'react';
import { apiBase } from '../api/apiBase';
import { apiFetch } from '../api/apiFetch';
import { setSessionToken } from '../api/sessionToken';

export interface AuthState {
  loading: boolean;
  authenticated: boolean;
  userId: string | null;
  appName: string | null;   // set only when authMode === 'app'
  profile: Record<string, unknown> | null;
  authMode: 'none' | 'app' | 'user';
  mode: 'debug' | 'production';
  // Set when the app_token → session exchange itself failed (e.g. invalid/
  // expired app_token, or a network error). Distinct from a plain "not
  // authenticated yet" state so the app-mode UI can show a real error
  // instead of silently looking empty.
  exchangeError: string | null;
}

const INITIAL: AuthState = {
  loading: true,
  authenticated: false,
  userId: null,
  appName: null,
  profile: null,
  authMode: 'none',
  mode: 'debug',
  exchangeError: null,
};

export function useAuth() {
  const [state, setState] = useState<AuthState>(INITIAL);

  const refresh = useCallback(async () => {
    try {
      const res = await apiFetch(`${apiBase}/api/auth/me`);
      if (res.ok) {
        const data = await res.json();
        setState({
          loading: false,
          authenticated: true,
          userId: data.userId,
          appName: data.appName ?? null,
          profile: data.profile ?? null,
          authMode: data.authMode,
          mode: data.mode,
          exchangeError: null,
        });
      } else {
        const data = await res.json().catch(() => ({}));
        setState(s => ({
          ...s,
          loading: false,
          authenticated: false,
          userId: null,
          appName: null,
          profile: null,
          authMode: data.authMode ?? s.authMode,
        }));
      }
    } catch {
      setState(s => ({ ...s, loading: false, authenticated: false }));
    }
  }, []);

  // On mount: check app_token in URL (app-to-app flow), then verify session
  useEffect(() => {
    const url = new URL(window.location.href);
    const appToken = url.searchParams.get('app_token');

    if (appToken) {
      // Exchange one-time token for a session. The response sets the
      // sofia.sid cookie (works for same-site/standalone use, unchanged)
      // AND returns sessionToken (used as a Bearer fallback so this also
      // works when the cookie can't survive a cross-site iframe embed).
      apiFetch(`${apiBase}/api/auth/exchange?app_token=${encodeURIComponent(appToken)}`)
        .then(async res => {
          // Clean token from URL without adding to history — it's one-time
          // and already consumed server-side regardless of outcome.
          url.searchParams.delete('app_token');
          window.history.replaceState({}, '', url.toString());

          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            setState(s => ({ ...s, loading: false, exchangeError: data.error ?? `HTTP ${res.status}` }));
            return;
          }

          const data = await res.json().catch(() => ({}));
          if (data.sessionToken) setSessionToken(data.sessionToken);
          return refresh();
        })
        .catch(() => setState(s => ({ ...s, loading: false, exchangeError: 'Network error' })));
    } else {
      refresh();
    }
  }, [refresh]);

  const login = useCallback(async (userId: string, password: string): Promise<string | null> => {
    try {
      const res = await apiFetch(`${apiBase}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, password }),
      });
      if (res.ok) {
        await refresh();
        return null;
      }
      const data = await res.json().catch(() => ({}));
      return (data.error as string) ?? 'Login failed';
    } catch {
      return 'Network error';
    }
  }, [refresh]);

  const logout = useCallback(async () => {
    await apiFetch(`${apiBase}/api/auth/logout`, { method: 'POST' });
    setSessionToken(null);
    setState(s => ({ ...s, authenticated: false, userId: null, profile: null }));
  }, []);

  return { ...state, refresh, login, logout };
}
