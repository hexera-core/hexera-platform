import { notFound } from "next/navigation";

import { isLaunchAdminSurfaceEnabled } from "@/app/_components/sections";
import { Alert, EmptyState, Panel } from "@/app/_components/panel";
import { compactDate, readActivity } from "@/lib/admin/read";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export default async function ActivityPage() {
  if (!isLaunchAdminSurfaceEnabled()) notFound();

  const activity = await readActivity();
  const runs = activity.data?.runs ?? [];

  return (
    <>
      <h1>Activity</h1>
      <p className="admin-lede">
        Recent runs across every organisation, newest first. Use this when launch traffic needs a
        quick answer before opening a tenant detail page.
      </p>

      <Panel title="Recent runs" heading="cross-tenant, newest first">
        {activity.error ? (
          <Alert>{activity.error}</Alert>
        ) : runs.length ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Created</th>
                <th>Status</th>
                <th>Organisation</th>
                <th>Owner</th>
                <th>Task</th>
                <th>Dispatch</th>
                <th>Attempts</th>
                <th>Heartbeat</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td>{compactDate(run.created_at)}</td>
                  <td>{run.status}</td>
                  <td>
                    {run.organization_id ? (
                      <a href={`/customers/${run.organization_id}`}>{run.organization_name || run.organization_id}</a>
                    ) : (
                      run.organization_name || "-"
                    )}
                  </td>
                  <td>{run.owner_id}</td>
                  <td>{run.task_label || "-"}</td>
                  <td>{run.pipeline_dispatch_state || run.pipeline_backend || "-"}</td>
                  <td>{run.attempts.toLocaleString("en-US")}</td>
                  <td>{compactDate(run.lease_heartbeat_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No runs have been submitted yet." />
        )}
      </Panel>
    </>
  );
}
