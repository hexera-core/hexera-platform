import assert from "node:assert/strict";
import test from "node:test";

import { decryptSecret, encryptSecret, isEncrypted, kmsKeyName } from "./crypto";

const KEY = "projects/p/locations/global/keyRings/outreach/cryptoKeys/tokens";

function cipher() {
  return {
    decrypt: async ({ ciphertext }: { ciphertext: Buffer }) => [
      { plaintext: Buffer.from(Buffer.from(ciphertext).toString("utf8").replace("ENC(", "").replace(")", "")) },
    ] as never,
    encrypt: async ({ plaintext }: { plaintext: Buffer }) => [
      { ciphertext: Buffer.from(`ENC(${Buffer.from(plaintext).toString("utf8")})`) },
    ] as never,
  };
}

test("a token round-trips through KMS", async () => {
  const sealed = await encryptSecret("1//refresh-token", KEY, cipher());
  assert.notEqual(sealed, "1//refresh-token");
  assert.equal(isEncrypted(sealed), true);
  assert.equal(await decryptSecret(sealed, KEY, cipher()), "1//refresh-token");
});

test("no plaintext token is ever written", async () => {
  // THE ASSERTION THE WHOLE MODULE EXISTS FOR. A database dump must not be a mailbox compromise.
  const sealed = await encryptSecret("1//super-secret-refresh", KEY, cipher());
  assert.ok(!sealed!.includes("super-secret-refresh"), "the stored value still contains the token");
});

test("it refuses to store a token when no key is configured", async () => {
  // Failing closed: writing the token in the clear because configuration is incomplete is the one
  // outcome worse than not writing it at all.
  await assert.rejects(() => encryptSecret("1//refresh", null, cipher()), /refusing to write it in the clear/i);
});

test("an already-sealed value is not double-encrypted", async () => {
  const once = await encryptSecret("token", KEY, cipher());
  assert.equal(await encryptSecret(once, KEY, cipher()), once);
});

test("a value written before this existed is still readable", async () => {
  // Rows imported from the laptop's database hold plaintext. They must be readable until they are
  // rewritten, and this must not look like a decryption failure.
  assert.equal(await decryptSecret("1//legacy-plaintext", KEY, cipher()), "1//legacy-plaintext");
  assert.equal(isEncrypted("1//legacy-plaintext"), false);
});

test("empty and absent values stay absent", async () => {
  for (const value of [null, undefined, ""]) {
    assert.equal(await encryptSecret(value, KEY, cipher()), null);
    assert.equal(await decryptSecret(value, KEY, cipher()), null);
  }
});

test("the key name comes from the environment", () => {
  assert.equal(kmsKeyName({ OUTREACH_KMS_KEY: KEY }), KEY);
  assert.equal(kmsKeyName({}), null);
});
