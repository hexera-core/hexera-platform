import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { formatDuration, runTitle, statusClass, statusLabel } from "@/app/_components/run-status";
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
        <div>
          <p className="page__eyebrow">Pipeline history</p>
          <h1 className="page__title">Mesh runs</h1>
          <p className="page__intro">
            Every submitted study, from intake through native meshing and final quality review.
          </p>
        </div>
        <Link className="btn btn--gold" href="/runs/new">
          New run
        </Link>
      </div>

      {page === null ? (
        // Fails soft, like every panel. A degraded API costs this table, never the page.
        <p className="empty card">Could not reach the API just now. Reload to try again.</p>
      ) : page.items.length === 0 ? (
        <p className="empty card">
          No runs yet. Upload a geometry file and describe the study you need — the first mesh
          takes minutes, not days.
        </p>
      ) : (
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">Run ledger</p>
              <p className="page__sub">Status, duration and attempts for each mesh pipeline.</p>
            </div>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Study</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Attempts</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((run) => (
                <tr key={run.id}>
                  <td>
                    <span className={statusClass(run.status, run.review_outcome)}>
                      {statusLabel(run.status, run.review_outcome)}
                    </span>
                  </td>
                  <td>
                    <Link href={`/runs/${run.id}`}>{runTitle(run)}</Link>
                  </td>
                  <td>
                    {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}
                  </td>
                  <td>{formatDuration(run.created_at, run.ended_at)}</td>
                  <td>{run.attempts}</td>
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
