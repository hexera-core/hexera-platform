import { EmptyState, Panel } from "@/app/_components/panel";
import type { ManagedInstanceRow } from "@/lib/gcp/instances";

import { ActionForm } from "./action-form";

// One row per instance. The TEMPLATE column is what makes a half-finished rotation legible: two
// template names in this table means the roll is still in flight.
//
// THE DELETE CONFIRMATION IS BLIND, AND SAYS SO. This console holds no database connection, so it
// cannot read job leases and cannot tell an operator that the instance they are about to remove is
// four hours into a mesh job. There is also no drain contract - the worker is not asked to finish
// first. Both facts are printed next to the button rather than left for the operator to discover.
export function InstanceTable({ rows }: { rows: readonly ManagedInstanceRow[] }) {
  const templates = new Set(rows.map((row) => row.templateName).filter(Boolean));

  return (
    <Panel
      heading={
        templates.size > 1 ? `${templates.size} template versions — rotation in flight` : undefined
      }
      title={`Instances (${rows.length})`}
    >
      {rows.length === 0 ? (
        <EmptyState note="The group is running no instances. At a floor of zero this is what idle looks like, and the first job of the day pays a full VM boot and image pull." />
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Instance</th>
              <th>Status</th>
              <th>Action</th>
              <th>Template</th>
              <th>Last error</th>
              <th>Operate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.name}>
                <td>{row.name}</td>
                <td>{row.status}</td>
                <td>{row.currentAction}</td>
                <td>{row.templateName ?? "—"}</td>
                <td>{row.lastErrors.join("; ") || "—"}</td>
                <td>
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
    </Panel>
  );
}
