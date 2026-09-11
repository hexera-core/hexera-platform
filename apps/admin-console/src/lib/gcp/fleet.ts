import type { AutoscalerReader, ManagerReader } from "./clients";
import type { FleetTarget } from "./config";

// The managed instance group and the autoscaler that sizes it, reduced to the numbers a person
// reads to answer "is the fleet doing the right thing".
//
// WHY THE AUTOSCALER'S STATUS IS A FIRST-CLASS FIELD. An autoscaler whose custom metric has no
// data reports CUSTOM_METRIC_INVALID and holds the group at its floor. Nothing fails, nothing
// pages, and the queue grows. create-worker-fleet.sh records the project having been through
// exactly that once. This is the only surface that says so.

export type ScalingPolicy = {
  cooldownSeconds: number | null;
  jobsPerInstance: number | null;
  maxReplicas: number | null;
  metricType: string | null;
  minReplicas: number | null;
  scaleInMaxReplicas: number | null;
  scaleInWindowSeconds: number | null;
};

export type AutoscalerHealth = {
  details: readonly { message: string; type: string }[];
  status: string;
};

export type FleetState = {
  // Read from the group, never guessed - see autoscalerNameFrom.
  autoscalerName: string | null;
  currentActions: Record<string, number>;
  health: AutoscalerHealth;
  isStable: boolean;
  policy: ScalingPolicy | null;
  targetSize: number;
  templateName: string | null;
};

// The counters worth naming. Listing them explicitly - rather than spreading whatever the API
// returned - means a new field in the proto cannot silently appear in the UI unlabelled.
const ACTION_KEYS = [
  "abandoning",
  "creating",
  "deleting",
  "none",
  "recreating",
  "refreshing",
  "restarting",
  "verifying",
] as const;

// A NOT_FOUND means the resource does not exist, which is a legitimate state - a fleet at a fixed
// size has no autoscaler. Any other code is a real failure and is rethrown, because rendering a
// permission error as "no scaling policy" states something untrue about the fleet.
//
// BOTH SPELLINGS, because the compute client is REST-backed and answers with an HTTP status while
// the gRPC clients answer with a status code. Checking only for 5 is what let a plain 404 escape
// this guard and take a whole page down with a raw error body instead of degrading one panel.
const NOT_FOUND_CODES = new Set<number | string>([5, 404]);

export function isNotFound(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  return NOT_FOUND_CODES.has((error as { code?: number | string }).code ?? -1);
}

// THE AUTOSCALER IS NOT NAMED AFTER ITS GROUP. `set-autoscaling` appends a suffix - the live one
// for `dev-workers` is `dev-workers-eyms` - so guessing the group's name gets a 404 for a group
// that has an autoscaler. The group's own `status.autoscaler` is the authoritative link, and it is
// the only thing that should ever be used to find it.
export function autoscalerNameFrom(manager: { status?: { autoscaler?: string | null } | null }): string | null {
  return lastSegment(manager.status?.autoscaler);
}

// Compute returns resource references as full self-links. The last segment is the name, which is
// the only part anyone reads.
export function lastSegment(selfLink: string | null | undefined): string | null {
  if (!selfLink) return null;
  const name = selfLink.split("/").pop();
  return name || null;
}

// int64 proto fields arrive as a number over REST, a string when they exceed a double, and a
// Long object over gRPC. All three reach this function, so all three are handled here rather than
// at each of the dozen call sites.
export type Numeric = number | string | { toNumber(): number } | null | undefined;

export function toNumber(value: Numeric): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "object") return value.toNumber();
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function readFleetState(
  clients: { autoscalers: AutoscalerReader; instanceGroupManagers: ManagerReader },
  target: FleetTarget,
): Promise<FleetState> {
  const [manager] = await clients.instanceGroupManagers.get({
    instanceGroupManager: target.migName,
    project: target.projectId,
    zone: target.migZone,
  });

  let policy: ScalingPolicy | null = null;
  let health: AutoscalerHealth = { details: [], status: "ABSENT" };

  const autoscalerName = autoscalerNameFrom(manager);

  try {
    if (!autoscalerName) throw Object.assign(new Error("this group has no autoscaler"), { code: 404 });
    const [autoscaler] = await clients.autoscalers.get({
      autoscaler: autoscalerName,
      project: target.projectId,
      zone: target.migZone,
    });

    const raw = autoscaler.autoscalingPolicy ?? {};
    // The fleet scales on exactly one custom metric - create-worker-fleet.sh builds a filter that
    // must select a single time series, which is the contract singleInstanceAssignment is defined
    // against. Reading the first entry is therefore reading the only entry.
    const custom = raw.customMetricUtilizations?.[0];

    policy = {
      cooldownSeconds: toNumber(raw.coolDownPeriodSec),
      jobsPerInstance: toNumber(custom?.singleInstanceAssignment),
      maxReplicas: toNumber(raw.maxNumReplicas),
      metricType: custom?.metric ?? null,
      minReplicas: toNumber(raw.minNumReplicas),
      scaleInMaxReplicas: toNumber(raw.scaleInControl?.maxScaledInReplicas?.fixed),
      scaleInWindowSeconds: toNumber(raw.scaleInControl?.timeWindowSec),
    };
    health = {
      details: (autoscaler.statusDetails ?? []).map((detail) => ({
        message: detail.message ?? "",
        type: detail.type ?? "UNKNOWN",
      })),
      status: autoscaler.status ?? "UNKNOWN",
    };
  } catch (error) {
    if (!isNotFound(error)) throw error;
  }

  const actions = manager.currentActions ?? {};

  return {
    autoscalerName,
    currentActions: Object.fromEntries(
      ACTION_KEYS.map((key) => [key, toNumber(actions[key]) ?? 0]),
    ),
    health,
    isStable: manager.status?.isStable ?? false,
    policy,
    targetSize: toNumber(manager.targetSize) ?? 0,
    templateName: lastSegment(manager.instanceTemplate),
  };
}
