import Link from "next/link";
import { NavLink } from "@/app/_components/nav-link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { formatDuration, runTitle, statusClass, statusLabel } from "@/app/_components/run-status";
import { formatCredits } from "@/app/_components/ledger";
import { When } from "@/app/_components/when-time";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Run = {
  id: string;
  status: string;
  task_label: string | null;
  is_rerun: boolean;
  created_at: string | null;
  ended_at: string | null;
  review_outcome?: string | null;
};

export default async function OverviewPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }

  // Both panels degrade independently: one failing must not blank the other, which is why these
  // are two awaited reads that each fail soft rather than one combined call.
  const [credits, runs] = await Promise.all([
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<{ items: Run[] }>("simulation?limit=5", ownerId),
  ]);

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1 className="page__title">Overview</h1>
          <p className="page__intro">
            Upload a part, answer a few questions, and get a mesh ready for your solver.
          </p>
        </div>
        <NavLink className="btn btn--gold" href="/runs/new">
          New run
        </NavLink>
      </div>

      <div className="summary-grid summary-grid--compact">
        <div className={`metric-card balance${credits && credits.balance <= 0 ? " balance--low" : ""}`}>
          <p className="label">Credits</p>
          <div className="balance__row">
            <b>{credits ? formatCredits(credits.balance) : "—"}</b>
            <span>{credits ? credits.unit : "unavailable"}</span>
          </div>
        </div>
      </div>

      <section aria-labelledby="recent-title" className="section">
        <div className="section__head">
          <h2 className="section__title" id="recent-title">Recent runs</h2>
          <Link className="section__link" href="/runs">
            All runs
          </Link>
        </div>
        <div className="panel">
        {runs === null ? (
          <DashboardNotice
            body="Refresh the page to try again. Your existing runs are not affected."
            retryLabel="Try again"
            title="We couldn't load recent runs"
            tone="error"
          />
        ) : runs.items.length === 0 ? (
          <DashboardNotice
            action={{ href: "/runs/new", label: "Start a run" }}
            body="Upload a geometry file and describe what the mesh is for."
            title="No runs yet"
          />
        ) : (
          <table className="table table--rows">
            <thead>
              <tr>
                <th className="col-main">Run</th>
                <th className="col-status">Status</th>
                <th className="col-when">Started</th>
                <th className="col-num col-opt">Duration</th>
              </tr>
            </thead>
            <tbody>
              {runs.items.map((run) => (
                <tr key={run.id}>
                  <td className="col-main">
                    <NavLink className="row-link" href={`/runs/${run.id}`}>{runTitle(run)}</NavLink>
                  </td>
                  <td>
                    <span className={statusClass(run.status, run.review_outcome)}>
                      {statusLabel(run.status, run.review_outcome)}
                    </span>
                  </td>
                  <td className="col-when">
                    <When iso={run.created_at} />
                  </td>
                  <td className="col-num col-opt">{formatDuration(run.created_at, run.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        </div>
      </section>
    </div>
  );
}
