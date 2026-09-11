import assert from "node:assert/strict";
import test from "node:test";

import {
  STATE_COOKIE,
  clearedStateCookie,
  consentUrl,
  exchangeUsable,
  newState,
  stateAccepted,
  stateCookie,
} from "./connect";
import type { OAuth2Client } from "google-auth-library";

// The real client builds the URL from the same options object, so a recorder is enough to assert
// on WHAT is asked for - which is the part that decides whether a refresh token comes back.
function recorder() {
  const seen: Record<string, unknown>[] = [];
  const client = {
    generateAuthUrl(options: Record<string, unknown>) {
      seen.push(options);
      return "https://accounts.google.com/o/oauth2/v2/auth?stubbed";
    },
  } as unknown as OAuth2Client;
  return { client, seen };
}

test("the consent request asks for the offline grant that yields a refresh token", () => {
  const { client, seen } = recorder();
  consentUrl(client, "state-value");

  assert.equal(seen[0].access_type, "offline");
  // Without this, a mailbox that consented before comes back with no refresh token and the sender
  // stops working an hour later.
  assert.equal(seen[0].prompt, "consent");
  assert.equal(seen[0].state, "state-value");
});

test("the consent request asks for send and modify, and nothing wider", () => {
  const { client, seen } = recorder();
  consentUrl(client, "s");

  const scopes = seen[0].scope as string[];
  assert.ok(scopes.includes("https://www.googleapis.com/auth/gmail.send"));
  assert.ok(scopes.includes("https://www.googleapis.com/auth/gmail.modify"));
  assert.ok(
    !scopes.some((scope) => scope.includes("mail.google.com")),
    "the full-mailbox scope would allow permanent deletion and is never requested",
  );
});

test("state is long, random, and not reused between attempts", () => {
  const first = newState();
  const second = newState();
  assert.notEqual(first, second);
  assert.ok(first.length >= 64, `expected at least 256 bits of hex, got ${first.length} chars`);
});

test("a callback is accepted only when its state matches the one this console issued", () => {
  assert.equal(stateAccepted("abc", "abc"), true);
  assert.equal(stateAccepted("abc", "abd"), false);
  // A forged callback arrives with no cookie at all. Treating an absent expectation as a match
  // would let any site hand this console someone else's authorization code.
  assert.equal(stateAccepted(null, "abc"), false);
  assert.equal(stateAccepted("abc", null), false);
  assert.equal(stateAccepted(null, null), false);
  assert.equal(stateAccepted("", ""), false);
});

test("the state cookie is host-only, script-invisible and short-lived", () => {
  const cookie = stateCookie("value", true);
  assert.ok(cookie.startsWith(`${STATE_COOKIE}=value`));
  assert.ok(cookie.includes("HttpOnly"));
  assert.ok(cookie.includes("Secure"));
  assert.ok(cookie.includes("Path=/outreach/auth"));
  // Lax, not Strict: the browser returns here as a top-level navigation from google.com, and
  // Strict withholds the cookie on exactly that request.
  assert.ok(cookie.includes("SameSite=Lax"));
});

test("the state cookie drops Secure only when the request was not over TLS", () => {
  assert.ok(!stateCookie("value", false).includes("Secure"));
  assert.ok(clearedStateCookie(true).includes("Secure"));
  assert.ok(clearedStateCookie(true).includes("Max-Age=0"));
});

test("an exchange with no refresh token anywhere is refused", () => {
  const result = exchangeUsable({ existingRefreshToken: null, refreshToken: null });
  assert.equal(result.ok, false);
  assert.match(result.reason ?? "", /refresh token/);
});

test("an exchange is usable when Google returns a refresh token", () => {
  assert.equal(exchangeUsable({ existingRefreshToken: null, refreshToken: "1//new" }).ok, true);
});

test("a re-consent that omits the refresh token is usable when one is already held", () => {
  // saveToken COALESCEs the absent value onto the stored one, so the mailbox keeps working.
  assert.equal(exchangeUsable({ existingRefreshToken: "1//held", refreshToken: undefined }).ok, true);
});
