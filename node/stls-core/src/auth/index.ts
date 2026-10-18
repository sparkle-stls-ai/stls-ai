export {
  AuthResult,
  LoaderBackend,
  KeyRecord,
  AuditEntry,
  ValidationResult,
  AuthConfig,
  hashKey,
  constantTimeEqual,
  generateApiKey,
  DEFAULT_KEY_TTL_SECONDS,
  DEFAULT_REFRESH_INTERVAL_SECONDS,
  REDIS_KEY_PREFIX,
  REDIS_REVOKED_SET,
  REDIS_INDEX_SET,
} from "./models.js";
export type { KeyRecordInit, AuditEntryInit, AuthConfigInit } from "./models.js";

export { StaticLoader, FileLoader, createLoader } from "./loaders.js";
export type { ApiKeyLoader, StaticKeyProps, CreateLoaderOptions } from "./loaders.js";

export { PostgresLoader } from "./loaders-postgres.js";
export type { PostgresLoaderOptions } from "./loaders-postgres.js";

export { RedisKeyCache } from "./store.js";

export {
  ApiKeyManager,
  ApiKeyManagerGroup,
  BackgroundRefresher,
  defaultAuditHook,
} from "./manager.js";
export type {
  AuditHook,
  RequestLike,
  ApiKeyManagerOptions,
  BackgroundRefresherOptions,
} from "./manager.js";
