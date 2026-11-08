import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { formatCredits, formatSigned, ledgerReason, shortRunId } from "@/app/_components/ledger";
import { NavLink } from "@/app/_components/nav-link";
import { runTitle } from "@/app/_components/run-status";
import { When } from "@/app/_components/when-time";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

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

export default async function UsagePage({
  searchParams,
}: {
  searchParams?: Promise<{ cursor?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const cursor = Array.isArray(params?.cursor) ? params.cursor[0] : params?.cursor;

  // Independent soft reads: a failing ledger must not blank the balance, and vice versa; the run
  // names only dress the ledger, so without them each run is shown by its short id.
  const [credits, ledger, runs] = await Promise.all([
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<LedgerPage>(
      `credits/history${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
      ownerId,
    ),
    consoleFetch<{ items: RunName[] }>(`simulation?limit=${NAMED_RUNS}`, ownerId),
  ]);
  const names = new Map<string, string>();
  for (const run of runs?.items ?? []) {
    if (run.task_label || run.is_rerun) {
      names.set(run.id, runTitle(run));
    }
  }
  const low = credits !== null && credits.balance <= 0;

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1 className="page__title">Usage</h1>
          <p className="page__intro">Credits are charged only for completed runs.</p>
        </div>
      </div>

      <div className="summary-grid summary-grid--compact">
        <div className={`metric-card balance${low ? " balance--low" : ""}`}>
          <p className="label">Balance</p>
          <div className="balance__row">
            <b>{credits ? formatCredits(credits.balance) : "—"}</b>
            <span>{credits ? credits.unit : "unavailable"}</span>
          </div>
          <div className="balance__foot">
            {low ? <span className="balance__warn">Below zero</span> : <span />}
            <Link className={low ? "btn btn--gold btn--sm" : "btn btn--sm"} href="/settings/billing">
              {low ? "Top up" : "Billing"}
            </Link>
          </div>
        </div>
      </div>

      {ledger === null ? (
        <DashboardNotice
          body="Refresh the page to try again. Your credit balance and history are not affected."
          retryLabel="Try again"
          title="We couldn't load usage history"
          tone="error"
        />
      ) : ledger.items.length === 0 ? (
        <DashboardNotice
          body="Credits added to or used by this workspace will appear here."
          title="No credit activity yet"
        />
      ) : (
        <section aria-labelledby="ledger-title" className="section">
          <h2 className="section__title" id="ledger-title">History</h2>
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
            <div className="section__more">
              <Link className="btn btn--sm" href={`/usage?cursor=${encodeURIComponent(ledger.next_cursor)}`}>
                Older movements
              </Link>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}
