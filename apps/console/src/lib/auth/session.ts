type ProviderSession = {
  subject?: string | null;
  emailVerified?: boolean;
  user?: {
    email?: string | null;
    name?: string | null;
  } | null;
};

export function ownerIdFromSession(session: ProviderSession | null): string | null {
  const email = session?.user?.email?.trim().toLowerCase();
  if (email) {
    return email;
  }

  return session?.subject?.trim() || null;
}

//: What the proxy answers an authenticated but unverified caller. Deliberately says which wall it
//: hit: the caller has already proven who they are, so naming the reason discloses nothing they
//: cannot already see on /verify-email, and "forbidden" with no cause reads as a broken console.
export const UNVERIFIED_REFUSAL = "Verify your email address to use the API.";

/**
 * The refusal a same-origin /api/v1 call earns before anything is proxied, or null to proceed.
 *
 * DECISION 4 IS AN AUTHORISATION BOUNDARY, NOT A REDIRECT. `(dashboard)/layout.tsx` consults
 * `emailVerified` during a server render, which decides what a browser is SHOWN and nothing more:
 * an unverified user sitting on /verify-email can open the console and `fetch("/api/v1/...")` with
 * their own session cookie, which the proxy would sign as them - minting a live API key, uploading
 * geometry, submitting jobs and spending the signup grant, all from behind the wall. The gate has
 * to live on the request path itself, which is here.
 *
 * NOTHING ON THE UNVERIFIED PATH LEGITIMATELY NEEDS THIS PROXY. Sign-up and the /verify-email
 * re-mint both reach the product API server-side through `authorizeFirebaseSession` -> POST
 * /auth/session; the only browser callers of /api/v1 are the API-keys panel and the workbench,
 * which live under (dashboard) and are already past the wall.
 *
 * An anonymous caller is NOT this function's business: it returns null and lets
 * `proxyProductApiRequest` answer its own 401, so the two refusals stay one each.
 */
export function proxyRefusal(session: ProviderSession | null): Response | null {
  if (!session || !ownerIdFromSession(session)) {
    return null;
  }
  if (session.emailVerified === true) {
    return null;
  }
  return Response.json({ error: UNVERIFIED_REFUSAL }, { status: 403 });
}
