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
          <p className="page__eyebrow">Overview</p>
          <h1 className="page__title">Your meshes</h1>
          <p className="page__intro">
            Upload a part, answer a few questions, and get a mesh ready for your solver.
          </p>
        </div>
        <Link className="btn btn--gold" href="/runs/new">
          New run
        </Link>
      </div>

      <div className="summary-grid">
        <div className="metric-card">
          <p className="label">Credits</p>
          <b>{credits ? credits.balance.toLocaleString() : "--"}</b>
          <span>{credits ? credits.unit : "unavailable"} available for meshing</span>
        </div>
        <div className="metric-card">
          <p className="label">Recent runs</p>
          <b>{runs ? runs.items.length.toLocaleString() : "--"}</b>
          <span>shown below, newest first</span>
        </div>
        <div className="metric-card">
          <p className="label">How it works</p>
          <b>CAD → Mesh</b>
          <span>Hexera plans the mesh, builds it in the cloud and checks it before you get it</span>
        </div>
      </div>

      <section className="panel">
        <div className="panel__head">
          <div>
            <p className="label">Recent runs</p>
            <p className="page__sub">Your latest runs and how they ended.</p>
          </div>
          <Link className="nav__link" href="/usage">
            Credit ledger
          </Link>
        </div>
        {runs === null ? (
          <p className="empty panel__body">Could not reach the API just now.</p>
        ) : runs.items.length === 0 ? (
          <p className="empty panel__body">
            Nothing has run yet. Press New run and upload a geometry file to start.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Run</th>
                <th>Duration</th>
              </tr>
            </thead>
            <tbody>
              {runs.items.map((run) => (
                <tr key={run.id}>
                  <td>
                    <span className={statusClass(run.status, run.review_outcome)}>
                      {statusLabel(run.status, run.review_outcome)}
                    </span>
                  </td>
                  <td>
                    <Link href={`/runs/${run.id}`}>{runTitle(run)}</Link>
                  </td>
                  <td>{formatDuration(run.created_at, run.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
