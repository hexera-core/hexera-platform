import { notFound } from "next/navigation";
import { randomUUID } from "node:crypto";

import { Alert, EmptyState, FieldList, Panel, StatRow } from "@/app/_components/panel";
import { isLaunchAdminSurfaceEnabled } from "@/app/_components/sections";
import { compactDate, readCustomer, signedNumber } from "@/lib/admin/read";
import { money, readInvoices, when } from "@/lib/billing/read";

import { grantCustomerCredits, raiseCustomerInvoice } from "../actions";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export default async function CustomerPage({
  params,
}: {
  params: Promise<{ organizationId: string }>;
}) {
  if (!isLaunchAdminSurfaceEnabled()) notFound();

  const { organizationId } = await params;
  const [customer, invoices] = await Promise.all([
    readCustomer(organizationId),
    readInvoices(organizationId),
  ]);
  const org = customer.data?.organization;
  const creditGrantOperationId = randomUUID();
  const invoiceOperationId = randomUUID();

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
              <tr><th>Created</th><th>Status</th><th>Owner</th><th>Task</th><th>Dispatch</th><th>Attempts</th></tr>
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
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="This organisation has no runs yet." />
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
    </>
  );
}
