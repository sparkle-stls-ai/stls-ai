// Augment express-session to add our fields
declare module 'express-session' {
  interface SessionData {
    // ── common ────────────────────────────────────────────────────────────────
    userType: 'user' | 'app';
    lastActivity: number;
    authMode: 'none' | 'app' | 'user';

    // ── user mode (VITE_AUTH_MODE=user) ───────────────────────────────────────
    // userId  = the login user_id (displayed in header, passed to agents)
    // profile = from sf_agents_access for the specific agent (set per-request)

    // ── app mode (VITE_AUTH_MODE=app) ─────────────────────────────────────────
    // appAuthUserId  = sf_user.user_id used for authentication (access control only)
    // appName        = sf_user.app_name  (displayed in header, in agent metadata)
    // userId         = caller_user_id supplied by the calling app (displayed, in metadata)
    // profile        = caller_profile supplied by the calling app (in metadata)

    userId:       string;                         // caller_user_id (app) | login user_id (user)
    appAuthUserId: string | null;                 // app mode only — sf_user credential
    appName:      string | null;                  // app mode only
    profile:      Record<string, unknown> | null; // caller_profile (app) | null (user — resolved per-agent)
  }
}

export interface SfUser {
  user_id: string;
  pwd_hash: string;
  type: 'user' | 'app';
  app_name: string | null;
  active: boolean;
}

export interface SfAgentConfig {
  agent_name: string;
  url: string;
  api_key: string | null;
}

export interface SfAgentAccess {
  agent_name: string;
  user_id: string;
  profile: Record<string, unknown> | null;
}

export interface SfAppToken {
  token: string;
  user_id: string;
  profile: Record<string, unknown> | null;
  expires_at: Date;
  used: boolean;
}
