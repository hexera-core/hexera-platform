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
