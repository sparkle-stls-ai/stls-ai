import dotenv from 'dotenv';
import path from 'path';

// Load .env from the project root (one level above server/)
dotenv.config({ path: path.resolve(__dirname, '../../.env') });

function required(key: string): string {
  const val = process.env[key];
  if (!val) throw new Error(`Missing required env var: ${key}`);
  return val;
}

function optional(key: string, fallback: string): string {
  return process.env[key] ?? fallback;
}

const rawMode = optional('VITE_MODE', 'debug') as 'debug' | 'production';
const rawAuth = optional('VITE_AUTH_MODE', 'none') as 'none' | 'app' | 'user';

export const config = {
  PORT: parseInt(optional('PORT', '3000'), 10),
  NODE_ENV: optional('NODE_ENV', 'development'),

  // Optional base path when served behind a path-based reverse proxy (e.g. /a).
  // Set BASE_PATH=/a at runtime; leave empty when served at the root.
  BASE_PATH: optional('BASE_PATH', '').replace(/\/+$/, ''),

  MODE: rawMode,
  AUTH_MODE: rawAuth,

  SESSION_SECRET: rawMode === 'production' ? required('SESSION_SECRET') : 'dev-secret-not-for-production',
  SESSION_TIMEOUT_MS: parseInt(optional('SESSION_TIMEOUT_MINUTES', '30'), 10) * 60 * 1000,

  // Space-separated list of origins allowed to embed this app in an <iframe>
  // (CSP frame-ancestors). Empty/unset (default) = no restriction, any site
  // can embed the app. Set to restrict embedding to specific origins once
  // known — see server/src/middleware/securityHeaders.ts for the tradeoff.
  // e.g. FRAME_ANCESTORS="https://bcame-portal.my.site.com https://*.force.com"
  FRAME_ANCESTORS: optional('FRAME_ANCESTORS', ''),

  // Suggested follow-up actions (LLM). Same env names used by Google ADK agents:
  //   GOOGLE_GENAI_USE_VERTEXAI=true  → Vertex AI (GOOGLE_CLOUD_PROJECT/LOCATION + ADC)
  //   otherwise                       → Gemini API key (GOOGLE_API_KEY)
  GOOGLE_GENAI_USE_VERTEXAI: /^(true|1)$/i.test(optional('GOOGLE_GENAI_USE_VERTEXAI', 'false')),
  GOOGLE_CLOUD_PROJECT: optional('GOOGLE_CLOUD_PROJECT', ''),
  GOOGLE_CLOUD_LOCATION: optional('GOOGLE_CLOUD_LOCATION', ''),
  GOOGLE_API_KEY: optional('GOOGLE_API_KEY', ''),
  SUGGESTIONS_MODEL: optional('SUGGESTIONS_MODEL', optional('AGENT_MODEL', 'gemini-3.5-flash')).trim(),

  DB_HOST: optional('DB_HOST', 'localhost'),
  DB_PORT: parseInt(optional('DB_PORT', '5432'), 10),
  DB_NAME: optional('DB_NAME', 'sofia'),
  DB_USER: optional('DB_USER', 'postgres'),
  DB_PASSWORD: optional('DB_PASSWORD', ''),

  // Python secure transport service (handles SecureA2ATransportAdapter calls)
  SECURE_TRANSPORT_URL: optional('SECURE_TRANSPORT_URL', 'http://localhost:8090'),
} as const;

export type AppMode = typeof config.MODE;
export type AuthMode = typeof config.AUTH_MODE;
