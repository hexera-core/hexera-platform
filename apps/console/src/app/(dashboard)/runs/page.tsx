import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { formatDuration, statusClass } from "@/app/_components/run-status";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Run = {
  id: string;
  status: string;
  task_label: string | null;
  created_at: string | null;
  ended_at: string | null;
  attempts: number;
  failed_reason: string | null;
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
        <Link className="btn btn--gold" href="/runs/new">
          New run
        </Link>
      </div>

      {page === null ? (
        // Fails soft, like every panel. A degraded API costs this table, never the page.
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : page.items.length === 0 ? (
        <p className="empty">
          No runs yet. Upload a geometry file and describe the study you need — the first mesh
          takes minutes, not days.
        </p>
      ) : (
        <>
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
                    <span className={statusClass(run.status)}>{run.status}</span>
                  </td>
                  <td>
                    <Link href={`/runs/${run.id}`}>{run.task_label ?? "Untitled study"}</Link>
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
            <Link
              className="btn"
              href={`/runs?cursor=${encodeURIComponent(page.next_cursor)}`}
            >
              Older runs
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
