import assert from "node:assert/strict";
import test from "node:test";

import { refreshVerifiedSession } from "./verify-email-refresh";

function deps(overrides = {}) {
  const calls: { forceRefresh: boolean[]; signedIn: string[] } = {
    forceRefresh: [],
    signedIn: [],
  };
  const user = {
    emailVerified: true,
    reload: async () => {},
    getIdToken: async (force: boolean) => {
      calls.forceRefresh.push(force);
      return "fresh-token";
    },
  };
  return {
    calls,
    dependencies: {
      currentUser: () => user,
      signIn: async (token: string) => {
        calls.signedIn.push(token);
        return { error: undefined as string | undefined };
      },
      ...overrides,
    },
  };
}

test("a verified user gets a session minted from a FORCE-REFRESHED token", async () => {
  // THE WHOLE POINT. getIdToken() without `true` returns the cached token, which still carries
  // email_verified: false -- so the wall would never lift for somebody who has genuinely
  // clicked the link. This assertion is the regression net for that silent failure.
  const { calls, dependencies } = deps();
  assert.equal(await refreshVerifiedSession(dependencies), "verified");
  assert.deepEqual(calls.forceRefresh, [true]);
  assert.deepEqual(calls.signedIn, ["fresh-token"]);
});

test("a user who has not clicked the link yet is told so and no session is minted", async () => {
  const { calls, dependencies } = deps({
    currentUser: () => ({
      emailVerified: false,
      reload: async () => {},
      getIdToken: async () => "unused",
    }),
  });
  assert.equal(await refreshVerifiedSession(dependencies), "still-unverified");
  assert.deepEqual(calls.signedIn, []);
});

test("a signed-out user is reported as such rather than throwing", async () => {
  // The Firebase SDK's currentUser is null after a page reload until it rehydrates, and null
  // after a real sign-out. Dereferencing it would throw inside a click handler.
  const { dependencies } = deps({ currentUser: () => null });
  assert.equal(await refreshVerifiedSession(dependencies), "signed-out");
});

test("reload runs before emailVerified is read", async () => {
  // emailVerified is a cached property on the SDK's user object. Reading it without reload()
  // returns whatever was true when the page loaded -- which is always false here.
  const order: string[] = [];
  const { dependencies } = deps({
    currentUser: () => ({
      get emailVerified() {
        order.push("read");
        return true;
      },
      reload: async () => {
        order.push("reload");
      },
      getIdToken: async () => "fresh-token",
    }),
  });
  await refreshVerifiedSession(dependencies);
  assert.deepEqual(order.slice(0, 2), ["reload", "read"]);
});

test("a session that will not mint is reported as still-unverified rather than as verified", async () => {
  const { dependencies } = deps({
    signIn: async () => ({ error: "CredentialsSignin" }),
  });
  assert.equal(await refreshVerifiedSession(dependencies), "still-unverified");
});
