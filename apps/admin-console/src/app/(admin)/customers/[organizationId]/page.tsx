import { randomUUID } from "node:crypto";

import { Alert, EmptyState, FieldList, Panel, StatRow } from "@/app/_components/panel";
import { compactDate, readCustomer, signedNumber } from "@/lib/admin/read";
import { manualPlanChoices, money, readInvoices, readPlans, when } from "@/lib/billing/read";

import { grantCustomerCredits, raiseCustomerInvoice, setCustomerManualPlan } from "../actions";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function seconds(value: number | null): string {
  if (value === null) return "-";
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

function payloadPreview(payload: unknown): string {
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

export default async function CustomerPage({
  params,
}: {
  params: Promise<{ organizationId: string }>;
}) {
  const { organizationId } = await params;
  const [customer, invoices, plans] = await Promise.all([
    readCustomer(organizationId),
    readInvoices(organizationId),
    readPlans(),
  ]);
  const org = customer.data?.organization;
  const creditGrantOperationId = randomUUID();
  const invoiceOperationId = randomUUID();
  const planChoices = manualPlanChoices(plans);

  if (customer.error) {
    return (
      <>
        <h1>Customer</h1>
        <Alert>{customer.error}</Alert>
      </>
    );
  }
  if (!customer.data || !org) {
    return (
      <>
        <h1>Customer</h1>
        <EmptyState note="Organisation not found." />
      </>
    );
  }

  return (
    <>
      <h1>{org.name}</h1>
      <p className="admin-lede">
        Organisation detail, recent runs, invoices and append-only credit controls.
      </p>

      <Panel title="Summary" heading={org.id}>
        <StatRow
          stats={[
            { label: "Balance", value: signedNumber(org.balance) },
            { label: "Runs", value: org.run_count.toLocaleString("en-US") },
            { label: "Active", value: org.active_runs.toLocaleString("en-US") },
          ]}
        />
        <FieldList
          fields={[
            { label: "Slug", value: org.slug },
            { label: "Plan", value: org.plan || "free" },
            { label: "Subscription", value: org.status || "none" },
            { label: "Included credits", value: org.included_credits.toLocaleString("en-US") },
            { label: "Stripe customer", value: org.stripe_customer_id || "-" },
            { label: "Created", value: compactDate(org.created_at) },
          ]}
        />
      </Panel>

      <Panel title="Credit controls" heading="append-only ledger grant">
        <form action={grantCustomerCredits} className="admin-form">
          <input type="hidden" name="organization_id" value={org.id} />
          <input type="hidden" name="operation_id" value={creditGrantOperationId} />
          <div className="admin-field-grid">
            <label className="admin-field-input">
              <span>Amount</span>
              <input name="amount" type="number" min="1" max="10000000" required />
              <small>Whole credits. Grants are positive only.</small>
            </label>
            <label className="admin-field-input">
              <span>Reason</span>
              <input name="reason" maxLength={128} required />
              <small>Stored on the ledger exactly as the customer support trail.</small>
            </label>
          </div>
          <button className="admin-button" type="submit">Grant credits</button>
        </form>
      </Panel>

      <Panel title="Manual billing plan" heading="operator-managed tier">
        {planChoices === null ? (
          <>
            <Alert>{plans.error ?? "The plan catalogue could not be read."}</Alert>
            <EmptyState note="Manual plan changes are disabled until the plan catalogue loads." />
          </>
        ) : (
          <form action={setCustomerManualPlan} className="admin-form">
            <input type="hidden" name="organization_id" value={org.id} />
            <div className="admin-field-grid">
              <label className="admin-field-input">
                <span>Plan</span>
                <select defaultValue={org.plan} name="plan">
                  <option value="">Free / no manual plan</option>
                  {planChoices.map((plan) => (
                    <option key={plan.name} value={plan.name}>
                      {plan.name}
                      {plan.purchasable ? "" : " (manual)"}
                    </option>
                  ))}
                </select>
                <small>A manual plan has no provider subscription status and can run beyond balance.</small>
              </label>
              <label className="admin-field-input">
                <span>Reason</span>
                <input name="reason" maxLength={128} required />
                <small>Recorded in the admin audit log.</small>
              </label>
            </div>
            <button className="admin-button" type="submit">Set manual plan</button>
          </form>
        )}
      </Panel>

      <Panel title="Invoices" heading="payment-provider records and manual billing">
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
                    {invoice.hosted_url ? (
                      <a href={invoice.hosted_url} rel="noreferrer" target="_blank">
                        open
                      </a>
                    ) : (
                      "-"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note={invoices.data?.reason ?? "This organisation has no invoices."} />
        )}

        <form action={raiseCustomerInvoice} className="admin-form">
          <input type="hidden" name="organization_id" value={org.id} />
          <input type="hidden" name="operation_id" value={invoiceOperationId} />
          <div className="admin-field-grid">
            <label className="admin-field-input">
              <span>Amount</span>
              <input name="amount" type="number" min="1" required />
              <small>Smallest currency unit, for example cents for USD.</small>
            </label>
            <label className="admin-field-input">
              <span>Currency</span>
              <input defaultValue="usd" maxLength={3} name="currency" required />
              <small>Three-letter provider currency code.</small>
            </label>
            <label className="admin-field-input">
              <span>Due in days</span>
              <input defaultValue={30} max={365} min={1} name="days_until_due" type="number" required />
              <small>Payment terms for the provider invoice.</small>
            </label>
            <label className="admin-field-input">
              <span>Description</span>
              <input maxLength={256} name="description" required />
              <small>Shown on the invoice sent by the provider.</small>
            </label>
          </div>
          <button className="admin-button" type="submit">
            Raise invoice
          </button>
        </form>
      </Panel>

      <Panel title="Members">
        {customer.data.members.length ? (
          <table className="admin-table">
            <thead>
              <tr><th>Name</th><th>Email</th><th>Role</th></tr>
            </thead>
            <tbody>
              {customer.data.members.map((member) => (
                <tr key={member.email}>
                  <td>{member.name}</td>
                  <td>{member.email}</td>
                  <td>{member.role}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No members are attached to this organisation." />
        )}
      </Panel>

      <Panel title="Recent runs" heading="newest first">
        {customer.data.recent_runs.length ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Created</th>
                <th>Status</th>
                <th>Owner</th>
                <th>Task</th>
                <th>Dispatch</th>
                <th>Attempts</th>
                <th>Duration</th>
                <th>Charged</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {customer.data.recent_runs.map((run) => (
                <tr key={run.id}>
                  <td>{compactDate(run.created_at)}</td>
                  <td>{run.status}</td>
                  <td>{run.owner_id}</td>
                  <td>{run.task_label || "-"}</td>
                  <td>{run.pipeline_dispatch_state || run.pipeline_backend || "-"}</td>
                  <td>{run.attempts.toLocaleString("en-US")}</td>
                  <td>{seconds(run.duration_seconds)}</td>
                  <td>{run.charged_credits.toLocaleString("en-US")}</td>
                  <td>
                    <a href={`/jobs/${run.id}/why`}>why</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="This organisation has no runs yet." />
        )}
      </Panel>

      <Panel title="Usage by period" heading="credits granted and spent for this organisation">
        {customer.data.usage_periods.length ? (
          <table className="admin-table">
            <thead>
              <tr><th>Period</th><th>Granted</th><th>Spent</th><th>Entries</th></tr>
            </thead>
            <tbody>
              {customer.data.usage_periods.map((period) => (
                <tr key={period.period ?? "unknown"}>
                  <td>{compactDate(period.period)}</td>
                  <td>{period.granted.toLocaleString("en-US")}</td>
                  <td>{period.spent.toLocaleString("en-US")}</td>
                  <td>{period.entries.toLocaleString("en-US")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No usage has been recorded for this organisation." />
        )}
      </Panel>

      <Panel title="Credit ledger" heading="newest first">
        {customer.data.ledger.length ? (
          <table className="admin-table">
            <thead>
              <tr><th>When</th><th>Type</th><th>Amount</th><th>Reason</th><th>Metered</th></tr>
            </thead>
            <tbody>
              {customer.data.ledger.map((entry) => (
                <tr key={entry.id}>
                  <td>{compactDate(entry.created_at)}</td>
                  <td>{entry.entry_type}</td>
                  <td>{signedNumber(entry.amount)}</td>
                  <td>{entry.reason || "-"}</td>
                  <td>{entry.metered_at ? compactDate(entry.metered_at) : entry.overage > 0 ? "pending" : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No credit ledger entries for this organisation." />
        )}
      </Panel>

      <Panel title="Traces" heading="recent durable capture operations">
        {customer.data.traces.length ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Seq</th>
                <th>When</th>
                <th>Job</th>
                <th>Generation</th>
                <th>Attempt</th>
                <th>Name</th>
                <th>Payload</th>
              </tr>
            </thead>
            <tbody>
              {customer.data.traces.map((trace) => (
                <tr key={trace.seq}>
                  <td>{trace.seq.toLocaleString("en-US")}</td>
                  <td>{compactDate(trace.created_at)}</td>
                  <td>{trace.job_id}</td>
                  <td>{trace.execution_generation.toLocaleString("en-US")}</td>
                  <td>{trace.attempt === null ? "-" : trace.attempt.toLocaleString("en-US")}</td>
                  <td>{trace.name || trace.record_type}</td>
                  <td>
                    <details>
                      <summary>{trace.kind}</summary>
                      <pre className="admin-json">{payloadPreview(trace.payload)}</pre>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No durable trace records have been captured for this organisation." />
        )}
      </Panel>
    </>
  );
}
