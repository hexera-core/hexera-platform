import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  type Catalogue,
  type Subscription,
  SALES_CONTACT,
  checkoutBanner,
  isPaying,
  planLabel,
  subscriptionSummary,
} from "@/app/_components/billing";
import { ExtraCreditsPicker, ManageBillingButton, PlanPicker } from "@/app/_components/billing-panel";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { formatSigned, ledgerReason } from "@/app/_components/ledger";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

// THIS PATH IS FIXED BY THE STRIPE ADAPTER: checkout's success and cancel URLs and the portal's
// return URL all point at /settings/billing (adapters/stripe_billing). Moving the page means moving
// those three with it, or a customer who has just paid lands on a 404.
//
// It is also the ONE billing surface: balance, plan, the charging rule and the ledger all live
// here, and /usage redirects in. The old split sent a "why was I charged" question across three
// pages; it is answered top to bottom on this one.
type Entry = {
  id: string;
  entry_type: string;
  amount: number;
  reason: string;
  created_at: string | null;
};

type LedgerPage = { items: Entry[]; next_cursor: string | null };

export default async function BillingPage({
  searchParams,
}: {
  searchParams?: Promise<{ checkout?: string | string[]; cursor?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const banner = checkoutBanner(
    Array.isArray(params?.checkout) ? params.checkout[0] : params?.checkout,
  );
  const cursor = Array.isArray(params?.cursor) ? params.cursor[0] : params?.cursor;

  // Four independent soft reads, so one failing panel does not blank the others.
  const [subscription, catalogue, credits, ledger] = await Promise.all([
    consoleFetch<Subscription>("billing", ownerId),
    consoleFetch<Catalogue>("billing/plans", ownerId),
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<LedgerPage>(
      `credits/history${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
      ownerId,
    ),
  ]);
  const paying = isPaying(subscription);

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Commercial control</p>
          <h1 className="page__title">Plans and credits</h1>
          <p className="page__intro">
            Manage the subscription that grants monthly capacity and add one-off credits when a
            study needs more room.
          </p>
        </div>
        {subscription?.has_billing_account ? <ManageBillingButton /> : null}
      </div>

      {banner ? (
        <div className={banner.tone === "ok" ? "card" : "auth__error"} role="status">
          {banner.text}
        </div>
      ) : null}

      <div className="summary-grid">
        <div className="metric-card">
          <p className="label">Current plan</p>
          <b>{subscription === null ? "Unavailable" : planLabel(paying ? subscription.plan : "")}</b>
          <span>{subscription === null ? "Plan data unavailable" : subscriptionSummary(subscription)}</span>
        </div>
        <div className="metric-card">
          <p className="label">Credit balance</p>
          <b>{credits ? credits.balance.toLocaleString() : "--"}</b>
          <span>{credits ? credits.unit : "unavailable"} available</span>
        </div>
      </div>

      <section className="panel">
        <div className="panel__head">
          <div>
            <p className="label">How you are charged</p>
          </div>
        </div>
        <div className="panel__body">
          <p className="page__sub">
            Credits are charged only for completed runs. A plan adds its credits when it renews,
            and on a paying plan use beyond them is billed at the end of the month.
          </p>
        </div>
      </section>

      <div className="billing-layout">
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">Plans</p>
              <p className="page__sub">Monthly capacity, limits and hosted Stripe plan changes.</p>
            </div>
          </div>
          {catalogue === null ? (
            <DashboardNotice
              body="Refresh the page to try again. Your current plan is not affected."
              retryLabel="Try again"
              title="We couldn't load available plans"
              tone="error"
            />
          ) : !catalogue.billing_enabled ? (
            <DashboardNotice
              action={{ href: SALES_CONTACT, label: "Contact us" }}
              body="Contact your Hexera representative to discuss plan options for this workspace."
              title="Online plan changes are unavailable"
            />
          ) : (
            <div className="panel__body">
              <PlanPicker
                billingEnabled={catalogue.billing_enabled}
                currentPlan={paying && subscription ? subscription.plan : ""}
                plans={catalogue.plans}
              />
            </div>
          )}
        </section>

        {catalogue?.extra_credits ? <ExtraCreditsPicker offer={catalogue.extra_credits} /> : null}
      </div>

      {ledger === null ? (
        <DashboardNotice
          body="Refresh the page to try again. Your credit balance and history are not affected."
          retryLabel="Try again"
          title="We couldn't load the credit ledger"
          tone="error"
        />
      ) : ledger.items.length === 0 ? (
        <DashboardNotice
          body="Credits added to or used by this workspace will appear here."
          title="No credit activity yet"
        />
      ) : (
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">Credit ledger</p>
              <p className="page__sub">Credits added or used by this workspace.</p>
            </div>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Type</th>
                <th>Amount</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {ledger.items.map((entry) => {
                const reason = ledgerReason(entry.reason);
                return (
                  <tr key={entry.id}>
                    <td>
                      {entry.created_at ? new Date(entry.created_at).toLocaleString() : "—"}
                    </td>
                    <td>
                      <span className="label">{entry.entry_type}</span>
                    </td>
                    <td>{formatSigned(entry.amount)}</td>
                    <td>
                      {reason.runId ? (
                        <Link href={`/runs/${reason.runId}`}>{reason.label}</Link>
                      ) : (
                        reason.label
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {ledger.next_cursor ? (
            <div className="panel__body">
              <Link
                className="btn"
                href={`/settings/billing?cursor=${encodeURIComponent(ledger.next_cursor)}`}
              >
                Older movements
              </Link>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}
