// The continue control's logic, kept free of React and of the Firebase SDK so it can be tested
// by calling it. The component below it does nothing but wire the real dependencies in.

export type VerifyUser = {
  emailVerified: boolean;
  reload: () => Promise<void>;
  getIdToken: (forceRefresh: boolean) => Promise<string>;
};

export type RefreshDeps = {
  currentUser: () => VerifyUser | null;
  signIn: (idToken: string) => Promise<{ error?: string }>;
};

export type RefreshOutcome = "verified" | "still-unverified" | "signed-out";

/** Re-mint the session against a freshly-refreshed ID token.
 *
 * THE SESSION DOES NOT LEARN ABOUT VERIFICATION ON ITS OWN. auth.ts's jwt callback copies
 * emailVerified from `user` at sign-in and nothing re-reads it per request, so a user who clicks
 * the link in their email still carries a JWT that says false. Two things are therefore
 * mandatory here and neither is optional:
 *
 *   reload()            - emailVerified is a CACHED property on the SDK's user object
 *   getIdToken(true)    - without the force flag the SDK hands back the cached token, whose
 *                         email_verified claim is still false, and the wall never lifts
 */
export async function refreshVerifiedSession(deps: RefreshDeps): Promise<RefreshOutcome> {
  const user = deps.currentUser();
  if (!user) {
    return "signed-out";
  }
  await user.reload();
  if (!user.emailVerified) {
    return "still-unverified";
  }
  const idToken = await user.getIdToken(true);
  const result = await deps.signIn(idToken);
  // A session that will not mint is NOT a verified session, whatever the address says. Reporting
  // "verified" here would send the user to a dashboard layout that bounces them straight back.
  return result?.error ? "still-unverified" : "verified";
}
