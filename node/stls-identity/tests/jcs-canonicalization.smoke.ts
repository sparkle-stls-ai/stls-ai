/**
 * jcs-canonicalization.smoke.ts
 * ------------------------------
 * Node mirror of python/stls-identity/tests/test_jcs_canonicalization.py.
 * Pure canonicalization-library checks — the cross-language byte-level
 * fixture itself lives in tests/interop/golden-vector.smoke.ts.
 */

import assert from "node:assert/strict";
import canonicalize from "canonicalize";
import { toPydanticIsoString } from "../src/models.js";

// T01 — non-ASCII must come through as raw UTF-8, not \uXXXX escapes
// (the exact bug class the real RFC 8785 library closes vs. a naive
// JSON.stringify(sortedKeys) stand-in).
{
  const payload = { client_id: "cliente-società-più" };
  const naive = JSON.stringify(payload, Object.keys(payload).sort());
  const real = canonicalize(payload)!;

  assert.ok(real.includes("società"), "RFC 8785 output must contain raw UTF-8, not \\u escapes");
  assert.ok(!real.includes("\\u"), "RFC 8785 requires raw UTF-8, no \\u escapes");
  console.log("OK: T01 non-ASCII canonicalizes to raw UTF-8");
  void naive;
}

// T02 — canonical output must be independent of object key insertion
// order (RFC 8785's defining property, and what makes independently
// implemented bridges converge on the same bytes).
{
  const a = canonicalize({ z: 1, a: 2, m: { y: 1, b: 2 } });
  const b = canonicalize({ a: 2, m: { b: 2, y: 1 }, z: 1 });
  assert.equal(a, b);
  console.log("OK: T02 canonicalization is independent of key insertion order");
}

// T03 — undefined/null handling: undefined keys are dropped (matches
// Pydantic's exclude_none=True dict shape), null values are kept as
// literal `null` (never silently dropped — only an ABSENT key should
// ever be omitted from a signed payload).
{
  const withUndefined = canonicalize({ a: 1, b: undefined });
  const withNull = canonicalize({ a: 1, b: null });
  assert.equal(withUndefined, '{"a":1}');
  assert.equal(withNull, '{"a":1,"b":null}');
  console.log("OK: T03 undefined keys dropped, null values kept literal");
}

// T04 — toPydanticIsoString() must match Pydantic v2's own round-trip
// datetime format exactly, confirmed empirically against a real
// Pydantic model: a zero sub-second fraction is DROPPED entirely
// (never emitted as ".000"), and any non-zero fraction is expanded to
// full 6-digit microseconds. This is a REAL bug this port caught: a
// credential/presentation signed with plain Date.toISOString() signs
// over bytes Python's own verifier (which re-validates then re-dumps
// before canonicalizing) will never reproduce, so every genuinely
// Node-issued credential would fail verification in Python — not a
// theoretical risk, confirmed by a live cross-runtime check during
// this package's development (see the package README).
{
  assert.equal(
    toPydanticIsoString(new Date("2026-01-15T12:00:00.000Z")),
    "2026-01-15T12:00:00Z",
    "an exact-zero millisecond fraction must be dropped, not emitted as .000",
  );
  assert.equal(
    toPydanticIsoString(new Date("2026-01-15T12:00:00.123Z")),
    "2026-01-15T12:00:00.123000Z",
    "a non-zero millisecond fraction must be expanded to 6-digit microseconds",
  );
  console.log("OK: T04 toPydanticIsoString() matches Pydantic's own datetime round-trip format");
}
