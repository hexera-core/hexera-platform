import { EmptyState, Panel } from "@/app/_components/panel";

// CUSTOMER BILLING - plans, credits, usage and invoices.
//
// THIS PAGE RENDERS NO FIGURES, ON PURPOSE. There are no `users`, `organizations` or
// `credit_ledger` tables, `settings/plans.py` defines `PLANS` as an empty dict, and identity today
// is a self-asserted `X-User-Id` header. A dashboard showing a plausible credit balance that was
// invented is worse than one that says the meter does not exist yet: the first is discovered to be
// fiction at the moment someone acts on it.
//
// What it does do is fix the SHAPE, so that landing accounts and metering (build-out plan items 4
// and 7) is wiring rather than redesign. Each panel states which table it will read.

export const dynamic = "force-static";

const PANELS = [
  {
    note:
      "Plan tiers and what each includes. `settings/plans.py` defines PLANS as an empty dict today, " +
      "so there are no tiers to show. The build-out plan recommends flat per-job tiers publicly and " +
      "measured resources internally, so pricing can move without a schema change.",
    title: "Plans",
  },
  {
    note:
      "Balance per account, derived from the ledger rather than stored. Arrives with the " +
      "`credit_ledger` table. A stored counter is the wrong shape here: jobs can run four hours " +
      "and fail, and a naive decrement leaks credits on every failure path.",
    title: "Credit balance",
  },
  {
    note:
      "Append-only grants, holds, debits and refunds — the entries a balance is summed from. " +
      "The hold-then-settle shape follows `NativeSubmissionClaim` and `persistence/lease.py`, which " +
      "is how the pipeline already thinks about work it has claimed but not finished.",
    title: "Ledger",
  },
  {
    note:
      "Jobs, mesh minutes and LLM tokens per account per period — what a bill would be computed " +
      "from. `InferenceCall` is currently bound to a Redis sink with a seven-day TTL, so there is " +
      "no durable record to total.",
    title: "Usage by period",
  },
  {
    note:
      "Issued invoices and their state. Last in line: it needs plans, a ledger and a billing " +
      "period before there is anything to issue.",
    title: "Invoices",
  },
];

export default function BillingPage() {
  return (
    <>
      <h1>Billing</h1>
      <p className="admin-lede">
        Customer billing. Nothing here exists yet — no accounts, no credit ledger, no durable usage
        record — so this page shows shape, not figures.
      </p>
      <p className="admin-lede">
        For what the platform itself costs to run, see <a href="/costs">Costs</a>.
      </p>

      {PANELS.map((panel) => (
        <Panel heading="arrives with accounts and metering" key={panel.title} title={panel.title}>
          <EmptyState note={panel.note} />
        </Panel>
      ))}
    </>
  );
}
