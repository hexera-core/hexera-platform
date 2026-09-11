/**
 * The login wall.
 *
 * This dashboard shows every contact, every message body, and the switch that
 * puts mail on the wire. On localhost that is fine, because reaching it means
 * already being on the machine. The moment it is reachable by a hostname, it
 * is not fine at all.
 *
 * So the rule is about the hostname, not the environment:
 *
 *   localhost / 127.0.0.1   open, exactly as it has always been
 *   any other host          a password is required
 *   any other host, and no password configured   refused outright
 *
 * That last line is the important one. Forgetting to set DASHBOARD_PASSWORD
 * before putting this behind a tunnel should produce a locked door, not an
 * open one.
 *
 * Sessions are a signed cookie rather than server state, so restarting the
 * worker or the dashboard does not log you out. The signing key is derived
 * from the password itself, which means changing the password invalidates
 * every existing session for free.
 */
import { createHmac, timingSafeEqual, randomBytes } from "node:crypto";

export const SESSION_COOKIE = "hexera_ops_session";
const SESSION_DAYS = 14;

export function dashboardPassword(): string | null {
  const value = process.env.DASHBOARD_PASSWORD?.trim();
  return value ? value : null;
}

/** True for requests that arrived at localhost, whatever the network path. */
export function isLocalHost(host: string | null | undefined): boolean {
  if (!host) return false;
  // Strip the port, and the brackets IPv6 literals arrive wrapped in.
  const name = host.replace(/^\[|\]$/g, "").split(":")[0]?.toLowerCase() ?? "";
  return name === "localhost" || name === "127.0.0.1" || name === "::1" || name === "";
}

function sign(payload: string, key: string): string {
  return createHmac("sha256", key).update(payload).digest("base64url");
}

/** `<expiry-ms>.<nonce>.<signature>` */
export function createSession(password: string, now = Date.now()): string {
  const expires = now + SESSION_DAYS * 24 * 60 * 60 * 1000;
  const payload = `${expires}.${randomBytes(9).toString("base64url")}`;
  return `${payload}.${sign(payload, password)}`;
}

export function sessionIsValid(
  token: string | undefined | null,
  password: string | null,
  now = Date.now(),
): boolean {
  if (!token || !password) return false;

  const cut = token.lastIndexOf(".");
  if (cut <= 0) return false;

  const payload = token.slice(0, cut);
  const signature = token.slice(cut + 1);
  if (!constantTimeEquals(signature, sign(payload, password))) return false;

  const expires = Number(payload.split(".")[0]);
  return Number.isFinite(expires) && expires > now;
}

/** Compares without leaking how much of the value matched, via timing. */
export function constantTimeEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  // timingSafeEqual throws on a length mismatch, which would itself be a
  // signal. Hash both to a fixed width first so every comparison is uniform.
  const norm = (buf: Buffer) => createHmac("sha256", "length-normalizer").update(buf).digest();
  return timingSafeEqual(norm(left), norm(right));
}

export function sessionCookieOptions(secure: boolean) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure,
    path: "/",
    maxAge: SESSION_DAYS * 24 * 60 * 60,
  };
}
