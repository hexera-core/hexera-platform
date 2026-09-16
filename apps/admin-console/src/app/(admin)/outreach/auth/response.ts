/**
 * Shared replies for the two mailbox-connect routes.
 *
 * Both ends of the consent round trip finish the same way: clear the state cookie and put the
 * person back on the Settings page with one sentence about what happened. Relative Location
 * headers are used deliberately - building an absolute URL would mean trusting a forwarded host
 * header behind IAP, and a redirect that resolves against the request the browser actually made
 * cannot be pointed somewhere else by a spoofed one.
 */
import { clearedStateCookie } from "@/lib/outreach/mail/connect";

const SETTINGS = "/outreach/settings";

/** Cloud Run terminates TLS at the edge, so the request reaching the container is plain http. */
export function isSecureRequest(request: Request): boolean {
  if (request.headers.get("x-forwarded-proto") === "https") return true;
  return new URL(request.url).protocol === "https:";
}

export function settingsRedirect(args: {
  connected?: string;
  error?: string;
  secure: boolean;
}): Response {
  const params = new URLSearchParams();
  if (args.connected) params.set("mailbox_connected", args.connected);
  // Truncated: this lands in a URL, and a multi-paragraph client-library error would push the
  // useful first clause off the end of anything that renders it.
  if (args.error) params.set("mailbox_error", args.error.slice(0, 300));

  const query = params.toString();
  return new Response(null, {
    headers: {
      Location: query ? `${SETTINGS}?${query}` : SETTINGS,
      "Set-Cookie": clearedStateCookie(args.secure),
    },
    status: 303,
  });
}
