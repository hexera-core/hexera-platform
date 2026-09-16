/**
 * Auth.js masks any authorize() failure that is not one of its own client-safe error types
 * (CredentialsSignin, AccessDenied, ...) down to a bare "Configuration" before it reaches the
 * browser -- see @auth/core's `clientErrors` allowlist. That masked bucket holds both a real
 * SignupDisabled (the API's 403) and any ordinary failure inside authorize() (HEXERA_API_BASE_URL
 * unreachable, a malformed response body, a bug), and the two are indistinguishable from here.
 *
 * An earlier version of this function guessed "this deployment is not accepting new accounts"
 * for the whole masked bucket. That guess is wrong whenever the real cause is an outage, and
 * because this function is shared by both SignInForm and SignUpForm, a user with valid
 * credentials hitting a backend outage during sign-in was told their non-existent account
 * problem was closed registration -- a misdiagnosis that hides a real outage instead of
 * reporting it. The fallback now reports honestly that something failed without asserting a
 * cause it cannot know. (The sign-up page still tells people signup is closed when it has real
 * evidence: its own server-side `auth.signup_enabled` check redirects before this form ever
 * renders.)
 */
export function explainSessionError(errorType: string, ordinaryFailureMessage: string): string {
  if (errorType === "CredentialsSignin") {
    return ordinaryFailureMessage;
  }
  return "Something went wrong. Try again in a moment.";
}

export function messageForSignUp(cause: unknown): string {
  const code = (cause as { code?: string })?.code ?? "";
  if (code === "auth/email-already-in-use") return "That email already has an account.";
  if (code === "auth/weak-password") return "Choose a longer password.";
  if (code === "auth/invalid-email") return "That does not look like an email address.";
  return "Could not create the account. Try again.";
}

export function messageForSignIn(cause: unknown): string {
  const code = (cause as { code?: string })?.code ?? "";
  if (code === "auth/invalid-email") return "That does not look like an email address.";
  if (code === "auth/too-many-requests") return "Too many attempts. Try again later.";
  // Firebase itself collapses "wrong password" and "no such account" into one code
  // (auth/invalid-credential) as its own enumeration protection; we echo that, not split it.
  if (
    code === "auth/invalid-credential" ||
    code === "auth/wrong-password" ||
    code === "auth/user-not-found"
  ) {
    return "Invalid email or password.";
  }
  return "Could not sign in. Try again.";
}
