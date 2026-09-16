/**
 * The "never contact" gate.
 *
 * Checked immediately before every send, after every other decision, so there
 * is exactly one chokepoint that can be audited. Domain-scope entries block
 * every address at a company — the correct response to "take us off your
 * list", which is a request from the organization, not just the individual.
 */
import { all, get, run } from "../db";
import { nowIso } from "./time";
import { recordEvent, EVENT_TYPES } from "./events";
import type { Suppression, SuppressionScope } from "./types";

export interface SuppressionCheck {
  suppressed: boolean;
  scope: SuppressionScope | null;
  reason: string | null;
  matchedValue: string | null;
}

export async function checkSuppressed(email: string | null): Promise<SuppressionCheck> {
  if (!email) return { suppressed: false, scope: null, reason: null, matchedValue: null };

  const normalized = email.trim().toLowerCase();
  const domain = normalized.slice(normalized.lastIndexOf("@") + 1);

  const match = await get<Suppression>(
    `SELECT * FROM suppressions
     WHERE (scope = 'email' AND value = ?) OR (scope = 'domain' AND value = ?)
     ORDER BY CASE scope WHEN 'email' THEN 0 ELSE 1 END
     LIMIT 1`,
    [normalized, domain],
  );

  if (!match) return { suppressed: false, scope: null, reason: null, matchedValue: null };
  return {
    suppressed: true,
    scope: match.scope,
    reason: match.reason,
    matchedValue: match.value,
  };
}

export async function suppress(options: {
  scope: SuppressionScope;
  value: string;
  reason: string;
  source: string;
  contactId?: number | null;
}): Promise<boolean> {
  const value = options.value.trim().toLowerCase();
  if (!value) return false;

  const result = await run(
    `INSERT INTO suppressions (scope, value, reason, source, contact_id, created_at)
     VALUES (?, ?, ?, ?, ?, ?)
     ON CONFLICT (scope, value) DO NOTHING`,
    [options.scope, value, options.reason, options.source, options.contactId ?? null, nowIso()],
  );

  if (result.changes > 0) {
    await recordEvent({
      type: EVENT_TYPES.suppressionAdded,
      entityType: "suppression",
      contactId: options.contactId ?? null,
      payload: { scope: options.scope, value, reason: options.reason, source: options.source },
    });
    return true;
  }
  return false;
}

export async function unsuppress(scope: SuppressionScope, value: string): Promise<boolean> {
  const normalized = value.trim().toLowerCase();
  const result = await run(`DELETE FROM suppressions WHERE scope = ? AND value = ?`, [scope, normalized]);
  if (result.changes > 0) {
    await recordEvent({
      type: EVENT_TYPES.suppressionRemoved,
      entityType: "suppression",
      payload: { scope, value: normalized },
    });
    return true;
  }
  return false;
}

export async function listSuppressions(): Promise<Suppression[]> {
  return await all<Suppression>(`SELECT * FROM suppressions ORDER BY created_at DESC`);
}

export function domainOf(email: string): string {
  return email.slice(email.lastIndexOf("@") + 1).toLowerCase();
}
