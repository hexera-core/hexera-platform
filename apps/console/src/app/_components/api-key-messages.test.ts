import assert from "node:assert/strict";
import test from "node:test";

import { mintFailureMessage, revokeOutcomeMessage } from "./api-key-messages";

test("mintFailureMessage reports the console-session meaning of a 403, not a generic failure", () => {
  // Someone holding a valid API key must not be told to "try again" when the truth is that key
  // management requires a console session -- their key still works.
  assert.equal(mintFailureMessage(403), "Only a console session can mint a key.");
});

test("mintFailureMessage falls back to an honest generic message for any other non-ok status", () => {
  assert.equal(mintFailureMessage(500), "Could not mint a key just now. Try again.");
  assert.equal(mintFailureMessage(404), "Could not mint a key just now. Try again.");
});

test("revokeOutcomeMessage reports the console-session meaning of a 403, not a generic failure", () => {
  assert.equal(revokeOutcomeMessage(403, false), "Only a console session can revoke a key.");
  // The 403 meaning holds regardless of whatever `revoked` value a caller might pass -- a 403
  // response never carries a `revoked` field at all.
  assert.equal(revokeOutcomeMessage(403, true), "Only a console session can revoke a key.");
});

test("revokeOutcomeMessage falls back to an honest generic message for any other non-ok status", () => {
  assert.equal(revokeOutcomeMessage(500, false), "Could not revoke that key. Try again.");
  assert.equal(revokeOutcomeMessage(404, false), "Could not revoke that key. Try again.");
});

test("revokeOutcomeMessage does not report a declined revocation as a success", () => {
  // DELETE answers 200 with {revoked: false} for a key id that does not exist OR one already
  // revoked. An ok status is not on its own evidence of a revocation.
  assert.equal(
    revokeOutcomeMessage(200, false),
    "That key was not revoked. It may already be revoked, or no longer exist.",
  );
});

test("revokeOutcomeMessage reports no message for an actual revocation", () => {
  assert.equal(revokeOutcomeMessage(200, true), null);
});
