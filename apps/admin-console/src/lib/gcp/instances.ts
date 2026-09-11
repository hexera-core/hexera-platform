import type { ManagerReader } from "./clients";
import type { FleetTarget } from "./config";
import { lastSegment } from "./fleet";

// Every instance the group manages, as a table row.
//
// `listManagedInstances` returns a PAGINATED TUPLE - [instances, nextRequest, rawResponse] - and
// the library resolves every page before returning the first element. A fleet is bounded by its
// ceiling, currently five, so there is no case here where that is the wrong trade.

export type ManagedInstanceRow = {
  currentAction: string;
  // The NUMERIC id, which is what per-instance Monitoring series are keyed by - `instance_id`, not
  // the name. Without it a worker's CPU and memory cannot be joined to the row describing it.
  id: string | null;
  // DETAILED health, when a health check is configured. `instanceHealth` is absent on groups with
  // no health checking, which is not the same as unhealthy and is not rendered as such.
  health: string | null;
  lastErrors: readonly string[];
  name: string;
  status: string;
  templateName: string | null;
  zone: string | null;
};

// A self-link is .../zones/<zone>/instances/<name>. The zone is the segment before "instances".
function zoneOf(selfLink: string | null | undefined): string | null {
  if (!selfLink) return null;
  const parts = selfLink.split("/");
  const index = parts.indexOf("zones");
  return index >= 0 ? (parts[index + 1] ?? null) : null;
}

export async function listFleetInstances(
  clients: { instanceGroupManagers: ManagerReader },
  target: FleetTarget,
): Promise<ManagedInstanceRow[]> {
  const [instances] = await clients.instanceGroupManagers.listManagedInstances({
    instanceGroupManager: target.migName,
    project: target.projectId,
    zone: target.migZone,
  });

  return (instances ?? [])
    .map((instance) => ({
      currentAction: instance.currentAction ?? "UNKNOWN",
      health: instance.instanceHealth?.[0]?.detailedHealthState ?? null,
      id: instance.id ? String(instance.id) : null,
      // The errors are nested twice: lastAttempt.errors is a wrapper whose own `errors` field is
      // the list. Reading one level less yields an object that renders as "[object Object]".
      lastErrors: (instance.lastAttempt?.errors?.errors ?? []).map((error) =>
        `${error.code ?? "ERROR"}: ${error.message ?? ""}`.trim(),
      ),
      name: lastSegment(instance.instance) ?? instance.name ?? "unknown",
      status: instance.instanceStatus ?? "UNKNOWN",
      templateName: lastSegment(instance.version?.instanceTemplate),
      zone: zoneOf(instance.instance) ?? target.migZone,
    }))
    // Compute returns instances in no guaranteed order. Sorting means a page that refreshes every
    // few seconds does not reshuffle its rows under the reader's cursor.
    .sort((a, b) => a.name.localeCompare(b.name));
}

export type FleetSummary = {
  byAction: readonly { count: number; label: string }[];
  byStatus: readonly { count: number; label: string }[];
  rotating: boolean;
  templateVersions: readonly string[];
  total: number;
  unhealthy: number;
};

// What the instance table adds up to. Counted here rather than in the component so the reading is
// testable and so "unhealthy" means one thing across the page.
export function summariseFleet(rows: readonly ManagedInstanceRow[]): FleetSummary {
  const tally = (values: readonly string[]) => {
    const counts = new Map<string, number>();
    for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
    return [...counts.entries()]
      .map(([label, count]) => ({ count, label }))
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
  };

  const templateVersions = [...new Set(rows.map((row) => row.templateName).filter((n): n is string => Boolean(n)))].sort();

  return {
    // NONE is the resting state of a managed instance and is not worth a chip of its own.
    byAction: tally(rows.map((row) => row.currentAction).filter((action) => action !== "NONE")),
    byStatus: tally(rows.map((row) => row.status)),
    // More than one template in flight means a rotation has not finished. Nothing else in the
    // console reports that, and it is the usual reason a fleet looks briefly oversized.
    rotating: templateVersions.length > 1,
    templateVersions,
    total: rows.length,
    // Only a health check that ran and said UNHEALTHY counts. A group with no health checking
    // reports null, which is unknown, not unhealthy.
    unhealthy: rows.filter((row) => row.health !== null && row.health !== "HEALTHY").length,
  };
}
