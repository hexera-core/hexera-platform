/**
 * Step 2 of connecting the sending mailbox: take Google's authorization code and store the grant.
 *
 * WHAT NEVER APPEARS HERE: the authorization code, the access token and the refresh token are
 * never written to a log line, an audit entry or a redirect parameter. The audit entry records
 * WHO connected WHICH mailbox with which scopes, which is the fact worth keeping; the credential
 * goes to the database sealed by KMS and nowhere else.
 */
import { cookies, headers } from "next/headers";

import { recordAdminAction } from "@/lib/audit";
import { getIapVerifier, iapAudience, verifyIapActor } from "@/lib/auth/iap";
import { readAdminTargets } from "@/lib/gcp/config";
import { STATE_COOKIE, exchangeUsable, stateAccepted } from "@/lib/outreach/mail/connect";
import { getProfileEmail, oauthClient, saveToken, storedToken } from "@/lib/outreach/mail/gmail";

import { isSecureRequest, settingsRedirect } from "../response";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const secure = isSecureRequest(request);
  const fail = (error: string) => settingsRedirect({ error, secure });
  let actor;
  try {
    // Inside the try: readAdminTargets throws on a deployment missing GCP_PROJECT_ID, and a
    // configuration fault should say so on the Settings page rather than render a 500.
    const targets = readAdminTargets(process.env);
    const headerList = await headers();
    actor = await verifyIapActor({
      assertion: headerList.get("x-goog-iap-jwt-assertion"),
      audience: iapAudience({
        projectNumber: targets.projectNumber,
        region: targets.region,
        service: targets.adminService,
      }),
      verifier: getIapVerifier(),
    });
  } catch (error) {
    return fail(error instanceof Error ? error.message : String(error));
  }

  const params = new URL(request.url).searchParams;

  // Google says no by redirecting here with an error rather than a code.
  const refusal = params.get("error");
  if (refusal) return fail(`Google did not grant access: ${refusal}`);

  const expected = (await cookies()).get(STATE_COOKIE)?.value ?? null;
  if (!stateAccepted(expected, params.get("state"))) {
    return fail(
      "This connection attempt could not be matched to one started here, so it was discarded. " +
        "Start again from this page.",
    );
  }

  const code = params.get("code");
  if (!code) return fail("Google returned no authorization code.");

  try {
    const client = oauthClient();
    const { tokens } = await client.getToken(code);
    client.setCredentials(tokens);

    // The mailbox identifies ITSELF. Taking the address from the token rather than from a form
    // means the record can never name an account other than the one the grant is actually for.
    const account = await getProfileEmail(client);

    const existing = await storedToken(account);
    const usable = exchangeUsable({
      existingRefreshToken: existing?.refresh_token,
      refreshToken: tokens.refresh_token,
    });
    if (!usable.ok) return fail(usable.reason ?? "The grant is not usable.");

    await saveToken(account, tokens as Record<string, unknown>);

    recordAdminAction({
      action: "outreach.mailbox.connected",
      actor: actor.email,
      after: { scopes: tokens.scope ?? null },
      resource: `gmail/${account}`,
    });

    return settingsRedirect({ connected: account, secure });
  } catch (error) {
    // Deliberately NOT the raw error: a failed token exchange from the Google client can echo the
    // request back, and that request carries the authorization code.
    console.error("outreach mailbox connect failed", error);
    return fail(
      "The mailbox could not be connected. The details are in this service's logs - the error is " +
        "withheld here because it can contain the authorization code.",
    );
  }
}
