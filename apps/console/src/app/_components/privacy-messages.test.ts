import assert from "node:assert/strict";
import { test } from "node:test";

import { confirmationMatches, erasureOutcomeMessage } from "./privacy-messages";

test("an accepted erasure needs no message", () => {
  assert.equal(erasureOutcomeMessage(202), null);
});

test("a refused confirmation says what to type", () => {
  assert.match(erasureOutcomeMessage(400) ?? "", /email address/);
});

test("a 409 shows the API's own sentence", () => {
  assert.equal(
    erasureOutcomeMessage(409, "Cancel your subscription under Billing before deleting the account."),
    "Cancel your subscription under Billing before deleting the account.",
  );
});

test("any other failure is generic", () => {
  assert.match(erasureOutcomeMessage(500) ?? "", /Try again/);
  assert.match(erasureOutcomeMessage(409) ?? "", /Try again/);
});

test("confirmation is case-insensitive and refuses an empty address", () => {
  assert.equal(confirmationMatches(" Ada@Example.com ", "ada@example.com"), true);
  assert.equal(confirmationMatches("ada@example.org", "ada@example.com"), false);
  assert.equal(confirmationMatches("", ""), false);
});
