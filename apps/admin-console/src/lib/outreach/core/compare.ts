import { createHmac, timingSafeEqual } from "node:crypto";

/** Compares without leaking how much of the value matched, via timing. */
export function constantTimeEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  // timingSafeEqual throws on a length mismatch, which would itself be a
  // signal. Hash both to a fixed width first so every comparison is uniform.
  const norm = (buf: Buffer) => createHmac("sha256", "length-normalizer").update(buf).digest();
  return timingSafeEqual(norm(left), norm(right));
}
