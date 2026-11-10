import Link from "next/link";
import { NavLink } from "@/app/_components/nav-link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { formatDuration, runTitle, statusClass, statusLabel } from "@/app/_components/run-status";
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
  attempts: number;
  failed_reason: string | null;
  review_outcome?: string | null;
};

type RunPage = { items: Run[]; next_cursor: string | null };

export default async function RunsPage({
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
  const page = await consoleFetch<RunPage>(
    `simulation${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    ownerId,
  );

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Runs</h1>
        <NavLink className="btn btn--gold" href="/runs/new">
          New run
        </NavLink>
      </div>

      {page === null ? (
        // Fails soft, like every panel. A degraded API costs this table, never the page.
        <DashboardNotice
          body="Refresh the page to try again. Your existing runs are not affected."
          retryLabel="Try again"
          title="We couldn't load your runs"
          tone="error"
        />
      ) : page.items.length === 0 ? (
        <DashboardNotice
          action={{ href: "/runs/new", label: "Start a run" }}
          body="Upload a geometry file and describe what the mesh is for."
          title="No runs yet"
        />
      ) : (
        <section className="panel">
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
              {page.items.map((run) => (
                <tr key={run.id}>
                  <td className="col-main">
                    {/* the title's link covers the whole row (dashboard.css .row-link) */}
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
                  <td className="col-num col-opt">
                    {formatDuration(run.created_at, run.ended_at)}
                    {/* tries only when there was more than one: the usual case says nothing */}
                    {run.attempts > 1 ? (
                      <span className="cell-faint" title={`${run.attempts} tries`}> · {run.attempts} tries</span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.next_cursor ? (
            <div className="panel__body">
              <Link
                className="btn"
                href={`/runs?cursor=${encodeURIComponent(page.next_cursor)}`}
              >
                Older runs
              </Link>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}
