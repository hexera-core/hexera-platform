import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  type Catalogue,
  type Subscription,
  SALES_CONTACT,
  chargingNote,
  checkoutBanner,
  isPaying,
  planLabel,
  subscriptionSummary,
} from "@/app/_components/billing";
import { BalanceChart, SpendChart } from "@/app/_components/billing-charts";
import { ExtraCreditsPicker, ManageBillingButton, PlanPicker } from "@/app/_components/billing-panel";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { formatCredits, formatSigned, ledgerReason, shortRunId } from "@/app/_components/ledger";
import {
  activityTotals,
  activityWindow,
  balanceSeries,
} from "@/app/_components/ledger-activity";
import { NavLink } from "@/app/_components/nav-link";
import { runTitle } from "@/app/_components/run-status";
import { When } from "@/app/_components/when-time";
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

type RunName = { id: string; task_label: string | null; is_rerun?: boolean };

/** The most runs one read names (the API's page limit). An older run beyond it is shown by its
 *  short id, still linked. */
const NAMED_RUNS = 100;

/** The ledger window the activity charts aggregate. The API's page limit bounds it, so the
 *  charts describe the recent picture and say so; a busier workspace keeps the full table
 *  (paginated below) for the rest. */
const ACTIVITY_LIMIT = 100;
const ACTIVITY_DAYS = 30;

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

  // Six independent soft reads, so one failing panel does not blank the others; the run names
  // only dress the ledger, so without them each run is shown by its short id.
  const [subscription, catalogue, credits, ledger, activity, runs] = await Promise.all([
    consoleFetch<Subscription>("billing", ownerId),
    consoleFetch<Catalogue>("billing/plans", ownerId),
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<LedgerPage>(
      `credits/history${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
      ownerId,
    ),
    consoleFetch<LedgerPage>(`credits/history?limit=${ACTIVITY_LIMIT}`, ownerId),
    consoleFetch<{ items: RunName[] }>(`simulation?limit=${NAMED_RUNS}`, ownerId),
  ]);
  const names = new Map<string, string>();
  for (const run of runs?.items ?? []) {
    if (run.task_label || run.is_rerun) {
      names.set(run.id, runTitle(run));
    }
  }
  const paying = isPaying(subscription);
  const days = activityWindow(activity?.items ?? [], new Date(), ACTIVITY_DAYS);
  const totals = activityTotals(days);
  // A leftover cursor means the read stopped before the ledger's beginning, so the balance line
  // is only drawn over days the window actually covers.
  const balances = credits
    ? balanceSeries(days, credits.balance, activity !== null && !activity.next_cursor)
    : [];
  const currentPlan = catalogue?.plans.find((candidate) => candidate.name === subscription?.plan);

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
          <span>
            {subscription === null
              ? "Plan data unavailable"
              : `${subscriptionSummary(subscription)}${currentPlan ? ` ${currentPlan.included_credits.toLocaleString()} credits a month.` : ""}`}
          </span>
        </div>
        <div className="metric-card">
          <p className="label">Credit balance</p>
          <b>{credits ? formatCredits(credits.balance) : "—"}</b>
          <span>{credits ? credits.unit : "unavailable"} available</span>
        </div>
        <div className="metric-card">
          <p className="label">Spent in the last 30 days</p>
          <b>{activity === null ? "—" : formatCredits(totals.spent)}</b>
          <span>
            {activity === null
              ? "activity unavailable"
              : `${formatCredits(totals.granted)} granted over the same window`}
          </span>
        </div>
      </div>

      <section aria-labelledby="plans-title" className="section">
        <div className="section__head">
          <h2 className="section__title" id="plans-title">Plans</h2>
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
          <PlanPicker
            billingEnabled={catalogue.billing_enabled}
            currentPlan={paying && subscription ? subscription.plan : ""}
            plans={catalogue.plans}
          >
            {catalogue.extra_credits ? <ExtraCreditsPicker offer={catalogue.extra_credits} /> : null}
          </PlanPicker>
        )}
      </section>

      {activity === null ? null : (
        <section aria-labelledby="activity-title" className="section">
          <div className="section__head">
            <h2 className="section__title" id="activity-title">Credit activity</h2>
            <p className="section__note">Last 30 days</p>
          </div>
          <div className="activity-grid">
            <section className="panel">
              <div className="panel__head">
                <p className="label">Spent and granted per day</p>
                <p className="chart-legend">
                  <i className="chart-legend__swatch chart-legend__swatch--spend" /> spent
                  <i className="chart-legend__swatch chart-legend__swatch--grant" /> granted
                </p>
              </div>
              <SpendChart days={days} />
            </section>
            <section className="panel">
              <div className="panel__head">
                <p className="label">Balance at end of day</p>
                {activity.next_cursor ? (
                  <p className="chart-legend">earlier history not shown</p>
                ) : null}
              </div>
              <BalanceChart days={days} series={balances} />
            </section>
          </div>
        </section>
      )}

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
        <section aria-labelledby="ledger-title" className="section">
          <h2 className="section__title" id="ledger-title">Credit ledger</h2>
          <div className="panel">
            <table className="table">
              <thead>
                <tr>
                  <th className="col-when">When</th>
                  <th className="col-main">For</th>
                  <th className="col-num col-right">Credits</th>
                </tr>
              </thead>
              <tbody>
                {ledger.items.map((entry) => {
                  const reason = ledgerReason(entry.reason);
                  return (
                    <tr key={entry.id}>
                      <td className="col-when">
                        <When iso={entry.created_at} />
                      </td>
                      <td className="col-main">
                        {reason.text ? <span>{reason.text}</span> : null}
                        {reason.text && reason.runId ? <span className="cell-faint"> · </span> : null}
                        {reason.runId ? (
                          names.has(reason.runId) ? (
                            <NavLink className="cell-link" href={`/runs/${reason.runId}`}>
                              {names.get(reason.runId)}
                            </NavLink>
                          ) : (
                            <NavLink className="cell-link" href={`/runs/${reason.runId}`}
                              title={reason.runId}>
                              Run <span className="cell-id">{shortRunId(reason.runId)}</span>
                            </NavLink>
                          )
                        ) : null}
                        {!reason.text && !reason.runId ? <span className="cell-faint">—</span> : null}
                      </td>
                      <td className={`col-num col-right amount${entry.amount > 0 ? " amount--in" : ""}`}
                        title={entry.entry_type}>
                        {formatSigned(entry.amount)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
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
          <p className="section__note">{chargingNote(subscription, catalogue)}</p>
        </section>
      )}
    </div>
  );
}
