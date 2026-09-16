import assert from "node:assert/strict";
import test from "node:test";

import { resumeInterruptedSignup } from "./signup-recovery";

const INPUT = {
  email: "engineer@acme.test",
  password: "correct horse battery",
  name: "An Engineer",
  company: "Acme Aerospace",
};

function deps(overrides = {}) {
  const calls: { names: string[]; forceRefresh: boolean[]; minted: string[] } = {
    names: [],
    forceRefresh: [],
    minted: [],
  };
  const identity = {
    setDisplayName: async (name: string) => {
      calls.names.push(name);
    },
    getIdToken: async (force: boolean) => {
      calls.forceRefresh.push(force);
      return "fresh-token";
    },
  };
  return {
    calls,
    dependencies: {
      signInWithPassword: async () => identity,
      mintSession: async (idToken: string, organizationName: string) => {
        calls.minted.push(`${idToken}|${organizationName}`);
        return { error: undefined as string | undefined };
      },
      ...overrides,
    },
  };
}

test("a resumed signup carries the company through to the session", async () => {
  // THE WHOLE POINT. Signing in instead of resuming loses the company, and the organisation is
  // then named after the email address with no way to rename it.
  const { calls, dependencies } = deps();
  assert.equal(await resumeInterruptedSignup(dependencies, INPUT), "resumed");
  assert.deepEqual(calls.minted, ["fresh-token|Acme Aerospace"]);
});

test("a resumed signup sets the display name before minting the token", async () => {
  // token.name is what carries the display name into users.name; a token minted first carries
  // the address instead, permanently.
  const order: string[] = [];
  const { dependencies } = deps({
    signInWithPassword: async () => ({
      setDisplayName: async () => {
        order.push("name");
      },
      getIdToken: async () => {
        order.push("token");
        return "fresh-token";
      },
    }),
  });
  await resumeInterruptedSignup(dependencies, INPUT);
  assert.deepEqual(order, ["name", "token"]);
});

test("a resumed signup forces a token refresh", async () => {
  const { calls, dependencies } = deps();
  await resumeInterruptedSignup(dependencies, INPUT);
  assert.deepEqual(calls.forceRefresh, [true]);
});

test("a rejected password is not recoverable and mints nothing", async () => {
  // Somebody else's address, or a typo. The caller falls back to the same message it would have
  // shown anyway, so this discloses nothing a plain sign-in attempt would not.
  const { calls, dependencies } = deps({ signInWithPassword: async () => null });
  assert.equal(await resumeInterruptedSignup(dependencies, INPUT), "not-recoverable");
  assert.deepEqual(calls.minted, []);
});

test("a session that will not mint is reported, not treated as resumed", async () => {
  const { dependencies } = deps({ mintSession: async () => ({ error: "Configuration" }) });
  assert.equal(await resumeInterruptedSignup(dependencies, INPUT), "session-failed");
});

test("a blank name skips the profile write rather than clearing it", async () => {
  const { calls, dependencies } = deps();
  await resumeInterruptedSignup(dependencies, { ...INPUT, name: "" });
  assert.deepEqual(calls.names, []);
});
