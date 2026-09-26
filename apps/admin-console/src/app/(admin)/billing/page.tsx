import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import {
  credits,
  money,
  planLabel,
  readInvoices,
  readLedger,
  readOrganizations,
  readPlans,
  readUsage,
  when,
  type BillingOrganization,
} from "@/lib/billing/read";

// CUSTOMER BILLING - plans, credits, usage and invoices.
//
// This page used to render no figures on purpose: there were no `organizations` or `credit_ledger`
// tables and `PLANS` was an empty dict, so every panel stated which table it was waiting for. Those
// tables exist now and the Stripe integration fills them, so each panel reads the thing it named.
//
// THE PANELS STILL DEGRADE INDIVIDUALLY, following Costs. They sit behind different states - a
// product API that is down, a deployment with no ADMIN_API_KEY, an account with no payment provider
// - and the original rule holds: a page that shows the three panels it could read and names what
// stopped the fourth beats one that renders a stack trace.
//
// WHAT IS SHOWN IS WHAT IS STORED. Nothing here totals anything itself: balances are summed by the
// ledger repository, allowances come from the plan catalogue, and invoice amounts are the payment
// provider's own. A figure computed twice is a figure that eventually disagrees with itself.

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

//: How many tenants get their ledger and invoices expanded on this page. Every organisation is
//: listed, but pulling the detail for all of them means one provider round trip each - so the page
//: expands the most recent few and links the rest to the provider's dashboard.
const DETAIL_LIMIT = 3;

export default async function BillingPage() {
  const [plans, organizations, usage] = await Promise.all([
    readPlans(),
    readOrganizations(),
    readUsage(),
  ]);

  const orgRows = organizations.data?.organizations ?? [];
  const detailed = orgRows.slice(0, DETAIL_LIMIT);

  // THE DETAIL READS ARE PARALLEL and each still returns its own error: one tenant whose invoices
  // cannot be fetched must not blank the ledger of the tenant above it.
  const details = await Promise.all(
    detailed.map(async (org) => ({
      invoices: await readInvoices(org.id),
      ledger: await readLedger(org.id),
      org,
    })),
  );

  const billingEnabled = plans.data?.billing_enabled ?? organizations.data?.billing_enabled ?? false;

  return (
    <>
      <h1>Billing</h1>
      <p className="admin-lede">
        Customer billing — what each organisation is on, what it has spent, and what it has been
        invoiced. For what the platform itself costs to run, see <a href="/costs">Costs</a>.
      </p>

      {billingEnabled ? null : (
        <Alert>
          No payment provider is configured on the product API (<code>STRIPE_API_KEY</code> is
          unset). Plans, ledger entries and balances below are real; invoices and checkout are not
          available until a key is set.
        </Alert>
      )}

      <Panel title="Plans" heading="settings/plans.py">
        {plans.error ? (
          <Alert>{plans.error}</Alert>
        ) : plans.data && plans.data.plans.length > 0 ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Plan</th>
                <th>Included credits</th>
                <th>Jobs / owner</th>
                <th>Concurrent</th>
                <th>Rate / min</th>
                <th>Purchasable</th>
              </tr>
            </thead>
            <tbody>
              {plans.data.plans.map((plan) => (
                <tr key={plan.name}>
                  <td>{plan.name}</td>
                  <td>{plan.included_credits.toLocaleString("en-US")}</td>
                  <td>{plan.max_jobs_per_owner.toLocaleString("en-US")}</td>
                  <td>{plan.max_concurrent_jobs.toLocaleString("en-US")}</td>
                  <td>{plan.rate_limit_per_minute.toLocaleString("en-US")}</td>
                  {/* A TIER WITH NO PRICE ID IS SHOWN, NOT HIDDEN. `enterprise` is invoiced by
                      hand and is deliberately unpurchasable; a tier that is merely unconfigured
                      looks identical here, and an operator needs to see both. */}
                  <td>{plan.purchasable ? "yes" : "no — invoiced or unconfigured"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="The plan catalogue is empty. settings/plans.py defines PLANS." />
        )}
      </Panel>

      <Panel title="Credit balance" heading="derived from credit_ledger, never stored">
        {organizations.error ? (
          <Alert>{organizations.error}</Alert>
        ) : orgRows.length > 0 ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Organisation</th>
                <th>Plan</th>
                <th>Subscription</th>
                <th>Balance</th>
                <th>Renews</th>
                <th>Provider customer</th>
              </tr>
            </thead>
            <tbody>
              {orgRows.map((org) => (
                <tr key={org.id}>
                  <td>{org.name}</td>
                  <td>{planLabel(org.plan)}</td>
                  <td>{subscriptionLabel(org)}</td>
                  <td>{credits(org.balance)}</td>
                  <td>{when(org.current_period_end)}</td>
                  {/* The provider's id is the join key an operator needs to open the same customer
                      in the provider's dashboard, which is where most of these questions end. */}
                  <td>{org.stripe_customer_id || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No organisations exist in this deployment yet." />
        )}
      </Panel>

      <Panel
        title="Usage by period"
        heading="credits granted and spent per month, across every tenant"
      >
        {usage.error ? (
          <Alert>{usage.error}</Alert>
        ) : usage.data ? (
          <>
            <StatRow
              stats={[
                {
                  label: "Unreported to meter",
                  value: `${usage.data.pending_meter.credits.toLocaleString("en-US")} credits`,
                },
                {
                  label: "Unreported entries",
                  value: usage.data.pending_meter.entries.toLocaleString("en-US"),
                },
              ]}
            />
            {/* THE BACKLOG IS THE CAVEAT ON THE TABLE BELOW. A figure that keeps climbing means
                consumption is being debited and never reaching an invoice - which nothing else in
                the product would say out loud. */}
            {usage.data.pending_meter.entries > 0 ? (
              <Alert tone="info">
                {usage.data.pending_meter.entries.toLocaleString("en-US")} debit(s) have not reached
                the provider&apos;s meter yet. The sweep reports them; a figure that keeps climbing
                means it is not running.
              </Alert>
            ) : null}
            {usage.data.periods.length > 0 ? (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Period</th>
                    <th>Granted</th>
                    <th>Spent</th>
                    <th>Entries</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.data.periods.map((period) => (
                    <tr key={period.period ?? "unknown"}>
                      <td>{when(period.period)}</td>
                      {/* GRANTED AND SPENT ARE SEPARATE COLUMNS, never netted: a month where
                          10,000 were granted and 10,000 spent is not a month where nothing
                          happened, and one net figure cannot tell them apart. */}
                      <td>{period.granted.toLocaleString("en-US")}</td>
                      <td>{period.spent.toLocaleString("en-US")}</td>
                      <td>{period.entries.toLocaleString("en-US")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState note="No ledger entries have been written yet." />
            )}
          </>
        ) : null}
      </Panel>

      <Panel title="Ledger" heading={`append-only entries, newest ${DETAIL_LIMIT} organisations`}>
        {details.length === 0 ? (
          <EmptyState note="No organisations to expand." />
        ) : (
          details.map(({ ledger, org }) => (
            <section className="admin-subsection" key={org.id}>
              <h3>{org.name}</h3>
              {ledger.error ? (
                <Alert>{ledger.error}</Alert>
              ) : ledger.data && ledger.data.entries.length > 0 ? (
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Type</th>
                      <th>Amount</th>
                      <th>Reason</th>
                      <th>Metered</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ledger.data.entries.map((entry) => (
                      <tr key={entry.id}>
                        <td>{when(entry.created_at)}</td>
                        <td>{entry.entry_type}</td>
                        <td>{credits(entry.amount)}</td>
                        <td>{entry.reason || "—"}</td>
                        {/* ONLY OVERAGE IS METERED. A debit with none is never reported - it was
                            paid from credits, or (no paid plan) it took the balance negative;
                            the Amount and the balance say which. Overage is pending until the
                            sweep stamps it.
                            This is the single most useful column when a bill looks too small. */}
                        <td>
                          {entry.entry_type !== "debit"
                            ? "n/a"
                            : entry.overage > 0
                              ? `${credits(entry.overage)} overage · ${entry.metered_at ? when(entry.metered_at) : "pending"}`
                              : "not metered"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <EmptyState note="This organisation has no ledger entries." />
              )}
            </section>
          ))
        )}
      </Panel>

      <Panel title="Invoices" heading="issued by the payment provider">
        {details.length === 0 ? (
          <EmptyState note="No organisations to expand." />
        ) : (
          details.map(({ invoices, org }) => (
            <section className="admin-subsection" key={org.id}>
              <h3>{org.name}</h3>
              {invoices.error ? (
                <Alert>{invoices.error}</Alert>
              ) : invoices.data && invoices.data.invoices.length > 0 ? (
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>Number</th>
                      <th>Issued</th>
                      <th>Status</th>
                      <th>Due</th>
                      <th>Paid</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {invoices.data.invoices.map((invoice) => (
                      <tr key={invoice.id}>
                        <td>{invoice.number || invoice.id}</td>
                        <td>{when(invoice.created_at)}</td>
                        <td>{invoice.status}</td>
                        <td>{money(invoice.amount_due, invoice.currency)}</td>
                        <td>{money(invoice.amount_paid, invoice.currency)}</td>
                        <td>
                          {/* THE PROVIDER'S OWN PAGE, never a PDF this product renders. A second
                              rendering of an invoice is a document that can disagree with the one
                              the customer was actually sent. */}
                          {invoice.hosted_url ? (
                            <a href={invoice.hosted_url} rel="noreferrer" target="_blank">
                              open
                            </a>
                          ) : (
                            "—"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <EmptyState
                  note={
                    invoices.data?.reason ??
                    "This organisation has no invoices."
                  }
                />
              )}
            </section>
          ))
        )}
      </Panel>
    </>
  );
}

function subscriptionLabel(org: BillingOrganization): string {
  // `past_due` IS NOT `canceled` and must not read as one: the first keeps serving while the card
  // is retried, and telling an operator it is cancelled sends them to reinstate an account that was
  // never off. An organisation with no subscription at all says so rather than showing a blank.
  if (org.status.trim() === "") {
    return org.has_billing_account ? "no subscription" : "never checked out";
  }
  return org.status;
}
