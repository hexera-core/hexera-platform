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
  return amount > 0 ? `+${amount}` : String(amount);
}

// A debit's reason carries the job that caused it ("mesh job <uuid>", "billed as overage: <uuid>"
// -- metering_service.JOB_REASON_PREFIX). The ledger is where somebody asks "why was I charged",
// so the id is pulled out and handed back for the page to link to the run rather than left as a
// uuid to paste into the address bar. Everything else passes through unchanged.
const RUN_REASON = /\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i;

export function ledgerReason(reason: string): { label: string; runId: string | null } {
  const match = RUN_REASON.exec(reason);
  if (!match) {
    return { label: reason || "—", runId: null };
  }
  const runId = match[1];
  const label = /^billed as overage/i.test(reason)
    ? `Billed as overage · run ${runId.slice(0, 8)}`
    : `Mesh run ${runId.slice(0, 8)}`;
  return { label, runId };
}
