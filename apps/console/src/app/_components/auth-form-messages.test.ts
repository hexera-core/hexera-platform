import assert from "node:assert/strict";
import test from "node:test";

import { explainSessionError, messageForSignIn, messageForSignUp } from "./auth-form-messages";

test("explainSessionError reports the ordinary failure message for a plain CredentialsSignin", () => {
  assert.equal(
    explainSessionError("CredentialsSignin", "Could not sign in. Try again."),
    "Could not sign in. Try again.",
  );
});

test("explainSessionError does not guess signup is closed for a masked (Configuration) error", () => {
  // Auth.js collapses everything authorize() throws that is not one of its own client-safe
  // types -- including a real SignupDisabled 403 and an ordinary outage inside authorize() --
  // down to "Configuration". The two are indistinguishable here, so the message must not assert
  // a specific cause (regression: this used to claim "not accepting new accounts" for any of
  // them, which misdiagnosed a backend outage during sign-in as closed registration).
  const message = explainSessionError("Configuration", "Could not sign in. Try again.");
  assert.doesNotMatch(message, /not accepting new accounts/i);
});

test("explainSessionError treats every non-CredentialsSignin type the same honest way", () => {
  // Not just "Configuration" -- whatever Auth.js's masked bucket is named, the function must not
  // invent a specific diagnosis for it.
  assert.equal(
    explainSessionError("SomeOtherAuthJsType", "ignored"),
    "Something went wrong. Try again in a moment.",
  );
});

test("messageForSignUp maps known Firebase codes to plain messages", () => {
  assert.equal(
    messageForSignUp({ code: "auth/email-already-in-use" }),
    "That email already has an account.",
  );
  assert.equal(messageForSignUp({ code: "auth/weak-password" }), "Choose a longer password.");
  assert.equal(
    messageForSignUp({ code: "auth/invalid-email" }),
    "That does not look like an email address.",
  );
});

test("messageForSignUp falls back to a generic message for an unrecognised or missing code", () => {
  assert.equal(
    messageForSignUp({ code: "auth/some-code-we-do-not-handle" }),
    "Could not create the account. Try again.",
  );
  assert.equal(messageForSignUp(new Error("boom")), "Could not create the account. Try again.");
  assert.equal(messageForSignUp(undefined), "Could not create the account. Try again.");
});

test("messageForSignIn collapses wrong-password and unknown-user into one message", () => {
  // Firebase itself no longer distinguishes these (its own enumeration protection); the mapping
  // must not reintroduce a distinction Firebase declined to make.
  assert.equal(messageForSignIn({ code: "auth/invalid-credential" }), "Invalid email or password.");
  assert.equal(messageForSignIn({ code: "auth/wrong-password" }), "Invalid email or password.");
  assert.equal(messageForSignIn({ code: "auth/user-not-found" }), "Invalid email or password.");
});

test("messageForSignIn maps rate limiting and malformed email distinctly", () => {
  assert.equal(
    messageForSignIn({ code: "auth/too-many-requests" }),
    "Too many attempts. Try again later.",
  );
  assert.equal(
    messageForSignIn({ code: "auth/invalid-email" }),
    "That does not look like an email address.",
  );
});

test("messageForSignIn falls back to a generic message for an unrecognised or missing code", () => {
  assert.equal(
    messageForSignIn({ code: "auth/some-code-we-do-not-handle" }),
    "Could not sign in. Try again.",
  );
  assert.equal(messageForSignIn(null), "Could not sign in. Try again.");
});
