import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { ownerIdFromSession, proxyRefusal, UNVERIFIED_REFUSAL } from "./session";

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

test("proxyRefusal refuses an authenticated but unverified session with a 403", async () => {
  // THE WALL IS AN AUTHORISATION BOUNDARY, NOT A REDIRECT. Without this, an unverified user
  // sitting on /verify-email can fetch("/api/v1/api-keys", {method:"POST"}) from the browser
  // console with their own session cookie and mint a live hx_live_ key.
  const refusal = proxyRefusal({
    user: { email: "unverified@example.com" },
    emailVerified: false,
  });
  assert.ok(refusal, "an unverified session must not reach the product API");
  assert.equal(refusal.status, 403);
  assert.deepEqual(await refusal.json(), { error: UNVERIFIED_REFUSAL });
});

test("proxyRefusal treats a session missing emailVerified as unverified", () => {
  // Fail closed. A JWT minted before emailVerified existed, or a callback that stopped copying
  // it, must not read as proof of an address nobody checked.
  const refusal = proxyRefusal({ user: { email: "legacy@example.com" } });
  assert.equal(refusal?.status, 403);
});

test("proxyRefusal lets a verified session through", () => {
  assert.equal(
    proxyRefusal({ user: { email: "verified@example.com" }, emailVerified: true }),
    null,
  );
});

test("proxyRefusal leaves the anonymous 401 to the proxy rather than answering 403", () => {
  // Two refusals, one each: "who are you" is proxyProductApiRequest's 401, and answering 403 to
  // a caller with no session would tell them verification is the thing standing in their way.
  assert.equal(proxyRefusal(null), null);
  assert.equal(proxyRefusal({ user: {} }), null);
});

test("the /api/v1 route actually consults proxyRefusal before it proxies", () => {
  // A pure function nobody calls is not a boundary. This reads the route module's own source so
  // deleting the two lines that wire it in fails here rather than silently reopening the API.
  const source = readFileSync(
    new URL("../../app/api/v1/[...path]/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /proxyRefusal\(session\)/);
  assert.ok(
    source.indexOf("proxyRefusal") < source.indexOf("proxyProductApiRequest(request"),
    "the refusal must be decided before the request is proxied",
  );
});
