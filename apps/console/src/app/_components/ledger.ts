/** A real minus sign (U+2212), not a hyphen: it lines up with "+" in a column of numbers. */
const MINUS = "−";

/** A credit count for reading: thousands grouped, a negative balance with a true minus sign. */
export function formatCredits(amount: number): string {
  const grouped = Math.abs(amount).toLocaleString("en-US");
  return amount < 0 ? `${MINUS}${grouped}` : grouped;
}

/** A ledger amount with its direction shown.
 *
 * `credit_ledger.amount` is signed by design -- a grant is positive and a debit negative, so the
 * balance is a plain SUM with no per-type arithmetic a new entry type could get wrong. Rendering
 * a positive amount without a "+" loses that symmetry in the one place a reader is comparing
 * the two.
 */
export function formatSigned(amount: number): string {
  if (amount === 0) {
    return "0";
  }
  return amount > 0 ? `+${formatCredits(amount)}` : formatCredits(amount);
}

const RUN_ID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** What a ledger reason says, split so the page can link the run it names.
 *
 * The metering service writes "mesh job <id>" for a run's charge and "billed as overage: <id>"
 * for the credits an invoiced overage puts back (application/metering_service.py). A raw uuid
 * in a table means nothing to a reader; the page names the run instead and links it. Anything
 * else is shown as written, first letter raised. */
export function ledgerReason(reason: string): { text: string; runId: string | null } {
  const raw = (reason || "").trim();
  const runId = raw.match(RUN_ID)?.[0] ?? null;
  if (runId && /^mesh job\b/i.test(raw)) {
    return { text: "", runId };
  }
  if (runId && /^billed as overage\b/i.test(raw)) {
    return { text: "Billed as overage", runId };
  }
  return { text: raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : "", runId: null };
}

/** The short name a run gets when the list has no label for it: the first 8 characters of its id. */
export function shortRunId(id: string): string {
  return id.slice(0, 8);
}
