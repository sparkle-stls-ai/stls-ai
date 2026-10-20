#!/usr/bin/env node
/**
 * Generate a bcrypt hash for a plaintext password.
 * Used to populate sf_user.pwd_hash.
 *
 * Usage:
 *   node scripts/hash-password.js <password>
 *
 * Example:
 *   node scripts/hash-password.js mysecretpassword
 */

const bcrypt = require('bcryptjs');

const password = process.argv[2];

if (!password) {
  console.error('Usage: node scripts/hash-password.js <password>');
  process.exit(1);
}

bcrypt.hash(password, 10).then(hash => {
  console.log(hash);
});
