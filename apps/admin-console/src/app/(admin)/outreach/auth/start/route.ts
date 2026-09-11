/**
 * Step 1 of connecting the sending mailbox: send the admin to Google's consent screen.
 *
 * A route handler rather than a Server Function because the browser has to LEAVE - a server action
 * returns a value to the page it was called from, and what is needed here is a redirect the user
 * agent follows to google.com and comes back from.
 */
import { headers } from "next/headers";

import { getIapVerifier, iapAudience, verifyIapActor } from "@/lib/auth/iap";
import { readAdminTargets } from "@/lib/gcp/config";
import { consentUrl, newState, stateCookie } from "@/lib/outreach/mail/connect";
import { oauthClient } from "@/lib/outreach/mail/gmail";

import { isSecureRequest, settingsRedirect } from "../response";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  // GRANTING THIS APP SEND-AS RIGHTS OVER A MAILBOX IS A PRIVILEGED CHANGE, so it is held to the
  // same bar as deleting a VM: a verified IAP assertion, not the plain-text header beside it.
  try {
    // Inside the try: readAdminTargets throws on a deployment missing GCP_PROJECT_ID, and a
    // configuration fault should say so on the Settings page rather than render a 500.
    const targets = readAdminTargets(process.env);
    const headerList = await headers();
    await verifyIapActor({
      assertion: headerList.get("x-goog-iap-jwt-assertion"),
      audience: iapAudience({
        projectNumber: targets.projectNumber,
        region: targets.region,
        service: targets.adminService,
      }),
      verifier: getIapVerifier(),
    });
  } catch (error) {
    return settingsRedirect({
      error: error instanceof Error ? error.message : String(error),
      secure: isSecureRequest(request),
    });
  }

  let url: string;
  const state = newState();
  try {
    url = consentUrl(oauthClient(), state);
  } catch (error) {
    // oauthClient() throws when GOOGLE_CLIENT_ID/SECRET are absent, which is a deployment that was
    // never given an OAuth client rather than anything the person did wrong.
    return settingsRedirect({
      error: error instanceof Error ? error.message : String(error),
      secure: isSecureRequest(request),
    });
  }

  return new Response(null, {
    headers: {
      Location: url,
      "Set-Cookie": stateCookie(state, isSecureRequest(request)),
    },
    status: 303,
  });
}
