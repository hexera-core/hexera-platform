import assert from "node:assert/strict";
import test from "node:test";

import { ownerIdFromSession } from "./session";

test("uses verified email as the backend owner id", () => {
  assert.equal(
    ownerIdFromSession({
      user: { email: " USER@Example.COM ", name: "User" },
    }),
    "user@example.com",
  );
});

test("falls back to provider subject when email is unavailable", () => {
  assert.equal(ownerIdFromSession({ user: {}, subject: "google-123" }), "google-123");
});

test("returns null when the provider session has no stable identity", () => {
  assert.equal(ownerIdFromSession({ user: { name: "User" } }), null);
});
