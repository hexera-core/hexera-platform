import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { authorizeFirebaseSession } from "./firebase-session.js";

const ENV = {
  HEXERA_API_BASE_URL: "http://api.test",
  MESH_API_KEY: "console-key",
};

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function stubFetch(status: number, body: unknown) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  globalThis.fetch = (async (url: string | URL, init: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return calls;
}

describe("authorizeFirebaseSession", () => {
  it("maps a verified token to the session the proxy already expects", async () => {
    stubFetch(200, {
      user_id: "user-1",
      owner_id: "person@example.com",
      organization_id: "org-1",
      email_verified: true,
      name: "A Person",
    });

    const user = await authorizeFirebaseSession({ idToken: "good" }, ENV);

    assert.deepEqual(user, {
      id: "person@example.com",
      email: "person@example.com",
      name: "A Person",
      organizationId: "org-1",
      emailVerified: true,
    });
  });

  it("identifies the console to the API with the mesh key", async () => {
    const calls = stubFetch(200, {
      user_id: "user-1",
      owner_id: "person@example.com",
      organization_id: "org-1",
      email_verified: true,
      name: "A Person",
    });

    await authorizeFirebaseSession({ idToken: "good" }, ENV);

    const headers = new Headers(calls[0].init.headers);
    assert.equal(headers.get("x-api-key"), "console-key");
    assert.equal(calls[0].url, "http://api.test/auth/session");
  });

  it("refuses a rejected token without throwing", async () => {
    stubFetch(401, { detail: "Invalid or expired sign-in token" });
    assert.equal(await authorizeFirebaseSession({ idToken: "forged" }, ENV), null);
  });

  it("refuses an absent token without calling the API", async () => {
    const calls = stubFetch(200, {});
    assert.equal(await authorizeFirebaseSession({}, ENV), null);
    assert.equal(calls.length, 0);
  });

  it("refuses rather than guessing when the API base URL is unset", async () => {
    const calls = stubFetch(200, {});
    assert.equal(
      await authorizeFirebaseSession({ idToken: "good" }, { MESH_API_KEY: "k" }),
      null,
    );
    assert.equal(calls.length, 0);
  });

  it("surfaces a closed signup distinctly so the page can explain it", async () => {
    stubFetch(403, { detail: "This deployment is not accepting new accounts" });
    await assert.rejects(
      () => authorizeFirebaseSession({ idToken: "good" }, ENV),
      /SignupDisabled/,
    );
  });
});
