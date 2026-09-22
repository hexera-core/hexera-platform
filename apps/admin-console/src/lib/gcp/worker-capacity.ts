import type { FleetState } from "./fleet";
import type { Series } from "./metrics";

export type WorkerCapacitySnapshot = {
  backlogBeyondCurrent: number | null;
  currentConfiguredCapacity: number | null;
  jobsPerInstance: number | null;
  latestQueueDepth: number | null;
  maxConfiguredCapacity: number | null;
  warmConfiguredCapacity: number | null;
};

function latestQueueDepth(queue: readonly Series[]): number | null {
  let total = 0;
  let found = false;

  for (const series of queue) {
    const point = series.points.at(-1);
    if (!point) continue;
    total += point.value;
    found = true;
  }

  return found ? total : null;
}

function capacity(instances: number | null | undefined, jobsPerInstance: number | null): number | null {
  if (instances === null || instances === undefined || jobsPerInstance === null) return null;
  return instances * jobsPerInstance;
}

export function workerCapacitySnapshot(
  state: FleetState,
  queue: readonly Series[],
): WorkerCapacitySnapshot {
  const jobsPerInstance = state.policy?.jobsPerInstance ?? null;
  const latestQueue = latestQueueDepth(queue);
  const current = capacity(state.targetSize, jobsPerInstance);

  return {
    backlogBeyondCurrent:
      latestQueue === null || current === null ? null : Math.max(0, latestQueue - current),
    currentConfiguredCapacity: current,
    jobsPerInstance,
    latestQueueDepth: latestQueue,
    maxConfiguredCapacity: capacity(state.policy?.maxReplicas, jobsPerInstance),
    warmConfiguredCapacity: capacity(state.policy?.minReplicas, jobsPerInstance),
  };
}
