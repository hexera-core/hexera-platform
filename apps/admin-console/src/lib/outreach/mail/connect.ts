/**
 * Connecting the sending mailbox, hosted.
 *
 * On a laptop this was `npm run gmail:auth`: a script that opened a browser, caught Google's
 * redirect on localhost:5789 and wrote the token to SQLite. None of that survives Cloud Run -
 * there is no browser to open, no loopback port to catch, and a filesystem that does not persist.
 * Without a replacement the deployed console renders every outreach page and can connect no
 * mailbox at all, which leaves sending, reply polling and the inbox permanently dead.
 *
 * So the consent round trip becomes two route handlers, and this module holds the parts that are
 * decisions rather than plumbing, so they can be tested without a live request.
 */
import { randomBytes } from "node:crypto";

import { constantTimeEquals } from "../core/compare";
import { GMAIL_SCOPES } from "./gmail";
import type { OAuth2Client } from "google-auth-library";

/** Scoped to the two routes that use it, so it is not attached to every console request. */
export const STATE_COOKIE = "outreach_oauth_state";
export const STATE_COOKIE_PATH = "/outreach/auth";

/** Long enough to read a consent screen, short enough that an abandoned attempt expires. */
const STATE_TTL_SECONDS = 600;

export function newState(): string {
  return randomBytes(32).toString("hex");
}

/**
 * The consent URL.
 *
 * `access_type=offline` with `prompt=consent` is the pair that gets a REFRESH token. Offline alone
 * is not enough: Google issues a refresh token only on a consent it considers new, so a mailbox
 * that was ever connected before comes back with an access token that expires in an hour and
 * nothing to renew it with. Forcing the prompt costs one extra click and is the difference between
 * a sender that runs unattended and one that dies at lunchtime.
 */
export function consentUrl(client: OAuth2Client, state: string): string {
  return client.generateAuthUrl({
    access_type: "offline",
    prompt: "consent",
    scope: GMAIL_SCOPES,
    state,
  });
}

export function stateCookie(state: string, secure: boolean): string {
  const parts = [
    `${STATE_COOKIE}=${state}`,
    `Path=${STATE_COOKIE_PATH}`,
    "HttpOnly",
    // Lax, not Strict: the browser arrives here as a top-level navigation FROM google.com, and
    // Strict would withhold the cookie on exactly that request - the one it exists to check.
    "SameSite=Lax",
    `Max-Age=${STATE_TTL_SECONDS}`,
  ];
  if (secure) parts.push("Secure");
  return parts.join("; ");
}

export function clearedStateCookie(secure: boolean): string {
  const parts = [`${STATE_COOKIE}=`, `Path=${STATE_COOKIE_PATH}`, "HttpOnly", "SameSite=Lax", "Max-Age=0"];
  if (secure) parts.push("Secure");
  return parts.join("; ");
}

/**
 * Is the state Google handed back the one this console issued?
 *
 * Without this, any page on the internet could send a logged-in admin to this callback carrying an
 * authorization code for an ATTACKER's mailbox, and the console would store it and start sending
 * from it. The cookie is the half the attacker cannot forge.
 */
export function stateAccepted(expected: string | null, received: string | null): boolean {
  if (!expected || !received) return false;
  return constantTimeEquals(expected, received);
}

/**
 * Whether an exchange leaves the mailbox usable unattended.
 *
 * Google returns a refresh token only on a fresh consent. Storing an access-token-only grant looks
 * like success and then stops working within the hour, so it is refused up front - unless a
 * refresh token for this mailbox is already held, which the save path preserves.
 */
export function exchangeUsable(args: {
  refreshToken: string | null | undefined;
  existingRefreshToken: string | null | undefined;
}): { ok: boolean; reason: string | null } {
  if (args.refreshToken || args.existingRefreshToken) return { ok: true, reason: null };
  return {
    ok: false,
    reason:
      "Google returned no refresh token, so this mailbox would stop sending within the hour. " +
      "Remove this app at myaccount.google.com/permissions and connect again.",
  };
}
