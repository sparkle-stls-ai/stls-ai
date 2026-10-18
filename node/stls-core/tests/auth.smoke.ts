import assert from "node:assert/strict";
import { writeFileSync, mkdtempSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import {
  ApiKeyManager,
  AuthResult,
  KeyRecord,
  hashKey,
  FileLoader,
  AuthConfig,
  StaticLoader,
} from "../src/auth/index.js";

const REDIS_URL = process.env.TEST_REDIS_URL ?? "redis://localhost:6399";

// 1) ApiKeyManager.fromStatic — OK / INVALID / EXPIRED / SCOPE_DENIED / REVOKED scenarios
{
  const manager = ApiKeyManager.fromStatic(
    {
      "good-key": { clientId: "agent-weather", scopes: ["secure-a2a"], endpoints: ["/secure-a2a"] },
    },
    true,
    { redisUrl: REDIS_URL },
  );
  await manager.start();

  const ok = await manager.validate("good-key", "/secure-a2a");
  assert.equal(ok.result, AuthResult.OK);
  assert.equal(ok.record?.clientId, "agent-weather");
  console.log("OK: validate() -> OK for a valid key on an authorized endpoint");

  const invalid = await manager.validate("wrong-key", "/secure-a2a");
  assert.equal(invalid.result, AuthResult.INVALID);
  console.log("OK: validate() -> INVALID for a nonexistent key");

  const scopeDenied = await manager.validate("good-key", "/admin");
  assert.equal(scopeDenied.result, AuthResult.SCOPE_DENIED);
  console.log("OK: validate() -> SCOPE_DENIED for an unauthorized endpoint");

  await manager.revoke(hashKey("good-key"));
  const revoked = await manager.validate("good-key", "/secure-a2a");
  assert.equal(revoked.result, AuthResult.REVOKED);
  console.log("OK: validate() -> REVOKED after revoke()");

  const health = await manager.health();
  assert.equal(health.redis, "ok");
  console.log("OK: health() reports redis=ok");

  await manager.stop();
}

// 2) KeyRecord with expiresAt in the past -> EXPIRED
{
  const expired = new KeyRecord({
    keyId: hashKey("expired-key"),
    clientId: "agent-old",
    expiresAt: new Date(Date.now() - 1000),
  });
  const loader = new StaticLoader([expired]);
  const manager = ApiKeyManager.fromLoader(loader, { redisUrl: REDIS_URL, enableCache: true });
  await manager.start();

  const result = await manager.validate("expired-key", "/secure-a2a");
  assert.equal(result.result, AuthResult.EXPIRED);
  console.log("OK: validate() -> EXPIRED for an expired key");

  await manager.stop();
}

// 3) FileLoader — JSON and YAML
{
  const dir = mkdtempSync(path.join(os.tmpdir(), "stls-auth-test-"));
  const jsonPath = path.join(dir, "keys.json");
  writeFileSync(
    jsonPath,
    JSON.stringify([{ raw_key: "file-key-json", client_id: "agent-json", scopes: ["secure-a2a"] }]),
  );

  const jsonLoader = new FileLoader(jsonPath);
  const jsonRecords = await jsonLoader.loadAll();
  assert.equal(jsonRecords.length, 1);
  assert.equal(jsonRecords[0].clientId, "agent-json");
  console.log("OK: FileLoader reads JSON with raw_key");

  const yamlPath = path.join(dir, "keys.yaml");
  writeFileSync(
    yamlPath,
    "keys:\n  - raw_key: file-key-yaml\n    client_id: agent-yaml\n    scopes: [secure-a2a]\n",
  );
  const yamlLoader = new FileLoader(yamlPath);
  const yamlRecords = await yamlLoader.loadAll();
  assert.equal(yamlRecords.length, 1);
  assert.equal(yamlRecords[0].clientId, "agent-yaml");
  console.log("OK: FileLoader reads YAML with raw_key (the 'yaml' package is installed)");
}

// 4) Static AuthConfig (bearer + api key)
{
  const config = new AuthConfig({ apiKeys: new Set(["abc123"]), bearerTokens: new Set(["tok456"]) });
  assert.equal(config.check(null, "abc123"), true);
  assert.equal(config.check("Bearer tok456", null), true);
  assert.equal(config.check(null, "wrong"), false);
  console.log("OK: AuthConfig.check() validates the api_key and the bearer token");
}

console.log("ALL AUTH SMOKE CHECKS PASSED");
