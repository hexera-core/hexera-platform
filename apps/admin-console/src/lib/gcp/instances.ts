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
