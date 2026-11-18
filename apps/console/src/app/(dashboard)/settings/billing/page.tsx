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
} from "@/app/_components/billing";
import { CreditsChart } from "@/app/_components/billing-charts";
import { ExtraCreditsPicker, ManageBillingButton, PlanPicker } from "@/app/_components/billing-panel";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { formatCredits, formatSigned, ledgerReason, shortRunId } from "@/app/_components/ledger";
import { spendByDay, spendTotal } from "@/app/_components/ledger-activity";
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

/** The chart's range choices, in days - the same segmented control every usage page teaches. */
const RANGE_CHOICES = [7, 30, 90] as const;
const DEFAULT_RANGE = 30;

export default async function BillingPage({
  searchParams,
}: {
  searchParams?: Promise<{
    checkout?: string | string[];
    cursor?: string | string[];
    range?: string | string[];
  }>;
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
  const rangeParam = Array.isArray(params?.range) ? params.range[0] : params?.range;
  const requested = rangeParam ? Number(rangeParam) : NaN;
  const range = (RANGE_CHOICES as readonly number[]).includes(requested) ? requested : DEFAULT_RANGE;

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
  // A ledger reason's run is the spend's source: its title when the run still names itself,
  // the short id when the name read did not reach it, "Other" for spend not tied to a run.
  const sourceOf = (entry: Entry) => {
    const runId = ledgerReason(entry.reason).runId;
    if (!runId) {
      return { key: "other", label: "Other" };
    }
    return { key: runId, label: names.get(runId) ?? `Run ${shortRunId(runId)}` };
  };
  const spend = spendByDay(activity?.items ?? [], sourceOf, new Date(), range);
  const spent = spendTotal(spend.days);
  const topSource = spend.sources[0] ?? null;

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
          <p className="label">Credits used</p>
          <b>{activity === null ? "—" : formatCredits(spent)}</b>
          <span>{activity === null ? "activity unavailable" : `In the last ${range} days`}</span>
        </div>
        <div className="metric-card">
          <p className="label">Top source</p>
          <b>{topSource ? topSource.label : "—"}</b>
          <span>
            {topSource ? `${formatCredits(topSource.amount)} in the last ${range} days` : "No credits used yet"}
          </span>
        </div>
        <div className="metric-card">
          <p className="label">Credit balance</p>
          <b>{credits ? formatCredits(credits.balance) : "—"}</b>
          <span>{credits ? `${credits.unit} available` : "unavailable"}</span>
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
            <h2 className="section__title" id="activity-title">Credits over time</h2>
            <nav className="range-tabs">
              {RANGE_CHOICES.map((choice) => (
                <Link
                  className={`range-tab${choice === range ? " range-tab--active" : ""}`}
                  href={`/settings/billing?range=${choice}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`}
                  key={choice}
                >
                  {choice} days
                </Link>
              ))}
            </nav>
          </div>
          <p className="page__sub">
            Daily credits by source, {spend.days[0]?.label} to {spend.days.at(-1)?.label}.
            {activity.next_cursor ? ` The ${ACTIVITY_LIMIT} newest ledger entries; older spend stays in the ledger below.` : ""}
          </p>
          <section className="panel">
            {spend.sources.length > 1 ? (
              <div className="panel__head">
                <p className="chart-legend">
                  {spend.sources.map((source, index) => (
                    <span className="chart-legend__item" key={source.key}>
                      <i className={`chart-legend__swatch chart-legend__swatch--s${index}`} />
                      {source.label}
                    </span>
                  ))}
                </p>
              </div>
            ) : null}
            {spent > 0 ? (
              <CreditsChart days={spend.days} sources={spend.sources} />
            ) : (
              <p className="chart-empty">No credits used in this range.</p>
            )}
          </section>
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
