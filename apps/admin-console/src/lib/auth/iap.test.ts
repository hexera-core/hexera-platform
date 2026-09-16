import assert from "node:assert/strict";
import test from "node:test";

import { iapAudience, verifyIapActor } from "./iap";

const KEYS = { pubkeys: { key1: "-----BEGIN PUBLIC KEY-----" } };

function verifier(payload: unknown, error?: Error) {
  return {
    getIapPublicKeys: async () => KEYS,
    verifySignedJwtWithCertsAsync: async () => {
      if (error) throw error;
      return { getPayload: () => payload };
    },
  } as never;
}

const AUDIENCE_ARGS = {
  projectNumber: "123456789",
  region: "us-central1",
  service: "hexera-dev-admin",
};

test("builds the audience Cloud Run's direct IAP signs for", () => {
  // Direct IAP on Cloud Run signs for the SERVICE, not for a backend service - that form belongs
  // to the load-balancer arrangement this deployment deliberately does not use.
  assert.equal(
    iapAudience(AUDIENCE_ARGS),
    "/projects/123456789/locations/us-central1/services/hexera-dev-admin",
  );
});

test("returns the identity IAP signed for", async () => {
  const actor = await verifyIapActor({
    assertion: "a.b.c",
    audience: iapAudience(AUDIENCE_ARGS),
    verifier: verifier({ email: "operator@hexera.ai", sub: "accounts.google.com:1234" }),
  });

  assert.deepEqual(actor, { email: "operator@hexera.ai", subject: "accounts.google.com:1234" });
});

test("refuses a request carrying no assertion", async () => {
  // The email header alone is trustworthy only while no allUsers binding exists. An audit trail
  // must not depend on a second script's invariant holding.
  await assert.rejects(
    () =>
      verifyIapActor({
        assertion: null,
        audience: iapAudience(AUDIENCE_ARGS),
        verifier: verifier({ email: "operator@hexera.ai" }),
      }),
    /no IAP assertion/i,
  );
});

test("refuses when the audience cannot be computed", async () => {
  // A deployment missing GCP_PROJECT_NUMBER cannot verify anything. Failing closed means writes
  // stop; failing open would mean accepting an unverified actor, which is the whole risk.
  await assert.rejects(
    () =>
      verifyIapActor({
        assertion: "a.b.c",
        audience: null,
        verifier: verifier({ email: "operator@hexera.ai" }),
      }),
    /audience/i,
  );
});

test("refuses an assertion that fails signature verification", async () => {
  await assert.rejects(
    () =>
      verifyIapActor({
        assertion: "a.b.c",
        audience: iapAudience(AUDIENCE_ARGS),
        verifier: verifier(null, new Error("Wrong recipient, payload audience != requiredAudience")),
      }),
    /Wrong recipient/,
  );
});

test("refuses a verified assertion that carries no email", async () => {
  await assert.rejects(
    () =>
      verifyIapActor({
        assertion: "a.b.c",
        audience: iapAudience(AUDIENCE_ARGS),
        verifier: verifier({ sub: "accounts.google.com:1234" }),
      }),
    /no email/i,
  );
});

test("passes the IAP issuer to the verifier, not a generic Google one", async () => {
  let seenIssuers: readonly string[] = [];
  const spy = {
    getIapPublicKeys: async () => KEYS,
    verifySignedJwtWithCertsAsync: async (
      _jwt: string,
      _certs: unknown,
      _audience: string,
      issuers: string[],
    ) => {
      seenIssuers = issuers;
      return { getPayload: () => ({ email: "operator@hexera.ai", sub: "s" }) };
    },
  } as never;

  await verifyIapActor({
    assertion: "a.b.c",
    audience: iapAudience(AUDIENCE_ARGS),
    verifier: spy,
  });

  assert.deepEqual(seenIssuers, ["https://cloud.google.com/iap"]);
});
