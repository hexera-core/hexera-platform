import { EmptyState, Panel } from "@/app/_components/panel";
import type { ManagedInstanceRow } from "@/lib/gcp/instances";

import { ActionForm } from "./action-form";

// One row per instance, and enough per-worker detail to answer "which one is the problem" without
// leaving the page.
//
// THE DELETE CONFIRMATION IS BLIND, AND SAYS SO. This console holds no database connection, so it
// cannot read job leases and cannot tell an operator that the instance they are about to remove is
// four hours into a mesh job. There is also no drain contract - the worker is not asked to finish
// first. Both facts are printed next to the button rather than left to be discovered.
//
// CPU and memory come from Monitoring keyed by the numeric instance id, not the name. Memory is
// published by the Ops Agent; a fleet without it has no series, which renders as an unknown rather
// than as zero - "using no memory" and "not reporting memory" are different statements.

export type InstanceMetrics = {
  cpu: Record<string, number>;
  memoryBytes: Record<string, number>;
  uptimeSeconds: Record<string, number>;
};

function percent(fraction: number | undefined): string {
  return fraction === undefined ? "—" : `${Math.round(fraction * 100)}%`;
}

function gigabytes(bytes: number | undefined): string {
  return bytes === undefined ? "—" : `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function duration(seconds: number | undefined): string {
  if (seconds === undefined) return "—";
  const hours = Math.floor(seconds / 3600);
  if (hours < 1) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function logsUrl(projectId: string, id: string): string {
  const query = `resource.type="gce_instance" resource.labels.instance_id="${id}"`;
  return `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(query)}?project=${projectId}`;
}

export function InstanceTable({
  metrics,
  projectId,
  rows,
}: {
  metrics: InstanceMetrics;
  projectId: string;
  rows: readonly ManagedInstanceRow[];
}) {
  return (
    <Panel title={`Instances (${rows.length})`}>
      {rows.length === 0 ? (
        <EmptyState note="The group is running no instances. At a floor of zero this is what idle looks like, and the first job of the day pays a full VM boot and image pull." />
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Instance</th>
              <th>State</th>
              <th>CPU</th>
              <th>Memory</th>
              <th>Uptime</th>
              <th>Template</th>
              <th>Last error</th>
              <th>Operate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.name}>
                <td>
                  {row.name}
                  {row.id ? (
                    <>
                      <br />
                      <small className="admin-subtle">{row.id}</small>
                    </>
                  ) : null}
                </td>
                <td>
                  {row.status}
                  {row.currentAction !== "NONE" ? (
                    <>
                      <br />
                      <small className="admin-subtle">{row.currentAction}</small>
                    </>
                  ) : null}
                  {/* Rendered only when a health check actually ran. No health checking is
                      unknown, and showing it as a state would be inventing one. */}
                  {row.health ? (
                    <>
                      <br />
                      <small className={row.health === "HEALTHY" ? "admin-subtle" : "admin-bad"}>
                        {row.health}
                      </small>
                    </>
                  ) : null}
                </td>
                <td>{percent(row.id ? metrics.cpu[row.id] : undefined)}</td>
                <td>{gigabytes(row.id ? metrics.memoryBytes[row.id] : undefined)}</td>
                <td>{duration(row.id ? metrics.uptimeSeconds[row.id] : undefined)}</td>
                <td>{row.templateName ?? "—"}</td>
                <td>{row.lastErrors.join("; ") || "—"}</td>
                <td>
                  {row.id ? (
                    <p style={{ margin: "0 0 0.4rem" }}>
                      <a href={logsUrl(projectId, row.id)} rel="noreferrer" target="_blank">
                        Logs
                      </a>
                    </p>
                  ) : null}
                  <ActionForm operation="instance.recreate" submitLabel="Recreate">
                    <input name="instance" type="hidden" value={row.name} />
                  </ActionForm>
                  <details className="admin-danger-zone">
                    <summary>Delete</summary>
                    <ActionForm danger operation="instance.delete" submitLabel="Delete instance">
                      <input name="instance" type="hidden" value={row.name} />
                      <p className="admin-empty">
                        This console cannot see whether {row.name} is running a job, and the worker
                        is not drained first. Anything in flight is lost. Type the name to confirm.
                      </p>
                      <label className="admin-field-input">
                        <span>Instance name</span>
                        <input autoComplete="off" name="confirm" placeholder={row.name} type="text" />
                      </label>
                    </ActionForm>
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="admin-empty" style={{ marginTop: "0.75rem" }}>
        CPU and memory are the most recent values Monitoring holds, keyed by instance id. Memory is
        published by the Ops Agent — a worker without it shows “—”, which means not reporting, not
        idle.
      </p>
    </Panel>
  );
}
