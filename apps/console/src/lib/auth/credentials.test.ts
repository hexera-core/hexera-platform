import assert from "node:assert/strict";
import test from "node:test";

import {
  hashConsolePassword,
  verifyConsoleCredentials,
  verifyConsolePassword,
} from "./credentials";

test("verifies a matching scrypt password hash", async () => {
  const hash = await hashConsolePassword("correct horse battery staple", "fixed-salt");

  assert.equal(await verifyConsolePassword("correct horse battery staple", hash), true);
});

test("rejects a mismatched scrypt password hash", async () => {
  const hash = await hashConsolePassword("correct horse battery staple", "fixed-salt");

  assert.equal(await verifyConsolePassword("wrong password", hash), false);
});

test("rejects malformed scrypt hashes without throwing", async () => {
  assert.equal(
    await verifyConsolePassword(
      "password",
      "scrypt:9007199254740991:8:1:fixed-salt:ZmFrZS1rZXk",
    ),
    false,
  );
});

test("authenticates a configured console user by normalized email", async () => {
  const passwordHash = await hashConsolePassword("login-password", "fixed-salt");

  const user = await verifyConsoleCredentials(
    {
      email: " USER@Example.COM ",
      password: "login-password",
    },
    {
      CONSOLE_AUTH_USERS: JSON.stringify([
        {
          email: "user@example.com",
          name: "Console User",
          passwordHash,
        },
      ]),
    },
  );

  assert.deepEqual(user, {
    id: "user@example.com",
    email: "user@example.com",
    name: "Console User",
  });
});

test("rejects unknown emails and malformed auth config", async () => {
  assert.equal(
    await verifyConsoleCredentials(
      { email: "missing@example.com", password: "login-password" },
      { CONSOLE_AUTH_USERS: "not-json" },
    ),
    null,
  );
});
