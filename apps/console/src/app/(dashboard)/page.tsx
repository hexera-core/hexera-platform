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
        <h1 className="page__title">Overview</h1>
        <Link className="btn btn--gold" href="/runs/new">
          New run
        </Link>
      </div>

      <div className="card">
        <p className="label">Credit balance</p>
        <p style={{ fontSize: "1.8rem", fontFamily: "var(--mono)" }}>
          {credits ? `${credits.balance} ${credits.unit}` : "unavailable"}
        </p>
        <Link className="nav__link" href="/usage">
          See the ledger
        </Link>
      </div>

      <div>
        <p className="label">Recent runs</p>
        {runs === null ? (
          <p className="empty">Could not reach the API just now.</p>
        ) : runs.items.length === 0 ? (
          <p className="empty">
            Nothing has run yet. Upload a geometry file to start your first study.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Study</th>
                <th>Duration</th>
              </tr>
            </thead>
            <tbody>
              {runs.items.map((run) => (
                <tr key={run.id}>
                  <td>
                    <span className={statusClass(run.status)}>{run.status}</span>
                  </td>
                  <td>
                    <Link href={`/runs/${run.id}`}>{run.task_label ?? "Untitled study"}</Link>
                  </td>
                  <td>{formatDuration(run.created_at, run.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
