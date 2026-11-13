/**
 * atrest.smoke.ts
 * ----------------
 * Node mirror of python/stls-core/tests/test_stls_core_atrest.py (A01-A11).
 *
 * Coverage (same IDs as the Python suite, for traceability):
 *   A01 — seal()/open() round-trip
 *   A02 — nonce is random: two seal() calls on the same plaintext differ
 *   A03 — tampered blob -> open() throws
 *   A04 — wrong KEK -> open() throws
 *   A05 — different labels (same KEK) derive independent keys
 *   A06 — fromEnv() returns undefined when the env var is unset
 *   A07 — fromEnv() throws on invalid base64 length
 *   A08 — fromEnv() throws when the decoded KEK isn't 32 bytes
 *   A09 — fromEnv() builds a working cipher when the env var is valid
 *   A10 — constructor rejects a KEK that isn't 32 bytes
 *   A11 — open() rejects a blob shorter than nonce+tag
 */

import assert from "node:assert/strict";
import { AtRestCipher } from "../src/atrest.js";

const KEK = Buffer.alloc(32, 0x11);
const LABEL = "stls-core/test";
const PLAINTEXT = Buffer.from('{"hello":"world"}');
const VAR_NAME = "STLS_CORE_ATREST_KEY_TEST";

// A01 — round-trip
{
  const cipher = new AtRestCipher(KEK, LABEL);
  assert.deepEqual(cipher.open(cipher.seal(PLAINTEXT)), PLAINTEXT);
  console.log("OK: A01 seal()/open() round-trip");
}

// A02 — nonce randomness
{
  const cipher = new AtRestCipher(KEK, LABEL);
  const blob1 = cipher.seal(PLAINTEXT);
  const blob2 = cipher.seal(PLAINTEXT);
  assert.notDeepEqual(blob1, blob2);
  console.log("OK: A02 nonce is random across seal() calls");
}

// A03 — tampered blob
{
  const cipher = new AtRestCipher(KEK, LABEL);
  const blob = Buffer.from(cipher.seal(PLAINTEXT));
  blob[blob.length - 1] ^= 0xff;
  assert.throws(() => cipher.open(blob));
  console.log("OK: A03 tampered blob -> open() throws");
}

// A04 — wrong KEK
{
  const sealed = new AtRestCipher(KEK, LABEL).seal(PLAINTEXT);
  const other = new AtRestCipher(Buffer.alloc(32, 0x22), LABEL);
  assert.throws(() => other.open(sealed));
  console.log("OK: A04 wrong KEK -> open() throws");
}

// A05 — different labels are independent
{
  const sealed = new AtRestCipher(KEK, "label-a").seal(PLAINTEXT);
  const other = new AtRestCipher(KEK, "label-b");
  assert.throws(() => other.open(sealed));
  console.log("OK: A05 different labels (same KEK) derive independent keys");
}

// A06 — fromEnv() unset returns undefined
{
  delete process.env[VAR_NAME];
  assert.equal(AtRestCipher.fromEnv(LABEL, VAR_NAME), undefined);
  console.log("OK: A06 fromEnv() unset returns undefined");
}

// A07 — fromEnv() invalid length (not valid base64 of 32 bytes)
{
  process.env[VAR_NAME] = "not-valid-base64-of-the-right-length";
  assert.throws(() => AtRestCipher.fromEnv(LABEL, VAR_NAME));
  delete process.env[VAR_NAME];
  console.log("OK: A07 fromEnv() invalid KEK -> throws");
}

// A08 — fromEnv() wrong decoded length
{
  process.env[VAR_NAME] = Buffer.alloc(16).toString("base64");
  assert.throws(() => AtRestCipher.fromEnv(LABEL, VAR_NAME));
  delete process.env[VAR_NAME];
  console.log("OK: A08 fromEnv() wrong KEK length -> throws");
}

// A09 — fromEnv() valid builds a working cipher
{
  process.env[VAR_NAME] = KEK.toString("base64");
  const cipher = AtRestCipher.fromEnv(LABEL, VAR_NAME);
  assert.notEqual(cipher, undefined);
  assert.deepEqual(cipher!.open(cipher!.seal(PLAINTEXT)), PLAINTEXT);
  delete process.env[VAR_NAME];
  console.log("OK: A09 fromEnv() valid KEK builds a working cipher");
}

// A10 — constructor rejects wrong KEK length
{
  assert.throws(() => new AtRestCipher(Buffer.alloc(16), LABEL));
  console.log("OK: A10 constructor rejects a KEK that isn't 32 bytes");
}

// A11 — open() rejects a short blob
{
  const cipher = new AtRestCipher(KEK, LABEL);
  assert.throws(() => cipher.open(Buffer.alloc(4)));
  console.log("OK: A11 open() rejects a blob shorter than nonce+tag");
}

console.log("ALL ATREST UNIT CHECKS PASSED");
