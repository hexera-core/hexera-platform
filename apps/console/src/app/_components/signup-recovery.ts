// Resuming a sign-up that got part-way through. Kept free of React and of the Firebase SDK so it
// can be tested by calling it; the form below wires the real dependencies in.

export type SignupIdentity = {
  setDisplayName: (name: string) => Promise<void>;
  getIdToken: (forceRefresh: boolean) => Promise<string>;
};

export type RecoveryDeps = {
  /** Sign in with the credentials just typed, or null if Identity Platform rejects them. */
  signInWithPassword: (email: string, password: string) => Promise<SignupIdentity | null>;
  mintSession: (idToken: string, organizationName: string) => Promise<{ error?: string }>;
};

export type RecoveryInput = {
  email: string;
  password: string;
  name: string;
  company: string;
};

export type RecoveryOutcome = "resumed" | "not-recoverable" | "session-failed";

/**
 * Finish a sign-up whose Identity Platform half already succeeded.
 *
 * WHY THIS EXISTS. `createUserWithEmailAndPassword` is the FIRST of five steps, and the four
 * after it - the display name, the verification mail, the token, the product account - can each
 * fail on their own. When one does, the credential exists but the product account may not, and
 * retrying sign-up is refused with `auth/email-already-in-use` forever. The obvious move,
 * signing in instead, works but silently loses the company: the sign-in form has no field for
 * it, and `POST /auth/session` honours `organization_name` only on the provisioning path, which
 * is the very path that had not run yet. The organisation is then named after the email address
 * and cannot be renamed, because renaming one is not in this cycle.
 *
 * So the recovery has to happen HERE, where the company typed into the form is still in hand.
 *
 * IT IS NOT AN ENUMERATION ORACLE. It runs only after Identity Platform has already said the
 * address is taken, and it discloses nothing a plain sign-in attempt would not: wrong password
 * returns `not-recoverable` and the caller shows the same "that email already has an account"
 * message it would have shown anyway.
 */
export async function resumeInterruptedSignup(
  deps: RecoveryDeps,
  { email, password, name, company }: RecoveryInput,
): Promise<RecoveryOutcome> {
  const identity = await deps.signInWithPassword(email, password);
  if (!identity) {
    // Someone else's address, or a typo in the password. Either way this is not a sign-up of
    // ours to resume.
    return "not-recoverable";
  }

  if (name) {
    // Before the token, exactly as the first attempt would have done it: `token.name` is what
    // carries the display name into `users.name`, and a token minted first carries the address.
    await identity.setDisplayName(name);
  }
  const idToken = await identity.getIdToken(true);
  const result = await deps.mintSession(idToken, company);
  // A session that will not mint leaves the account exactly as stuck as it was; saying so beats
  // reporting a success the user cannot act on.
  return result?.error ? "session-failed" : "resumed";
}
