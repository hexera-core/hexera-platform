import { FieldList, Panel, StatRow } from "@/app/_components/panel";
import type { FleetState } from "@/lib/gcp/fleet";
import type { WorkerProfile } from "@/lib/gcp/worker-profile";
import {
  workerCapacitySnapshot,
  type WorkerCapacitySnapshot,
} from "@/lib/gcp/worker-capacity";
import type { Series } from "@/lib/gcp/metrics";

function show(value: number | null, suffix = ""): string {
  return value === null ? "unknown" : `${value}${suffix}`;
}

function pressure(snapshot: WorkerCapacitySnapshot): string {
  if (snapshot.latestQueueDepth === null || snapshot.currentConfiguredCapacity === null) return "unknown";
  if (snapshot.latestQueueDepth === 0) return "idle";
  if (snapshot.backlogBeyondCurrent && snapshot.backlogBeyondCurrent > 0) return "over current target";
  return "within current target";
}

export function WorkerCapacityPanel({
  profile,
  queue,
  state,
}: {
  profile: WorkerProfile | null;
  queue: readonly Series[];
  state: FleetState;
}) {
  const snapshot = workerCapacitySnapshot(state, queue);

  return (
    <Panel heading={pressure(snapshot)} title="Worker capacity">
      <StatRow
        stats={[
          { label: "Queue now", value: show(snapshot.latestQueueDepth) },
          { label: "Current capacity", value: show(snapshot.currentConfiguredCapacity) },
          { label: "Ceiling capacity", value: show(snapshot.maxConfiguredCapacity) },
          { label: "Warm capacity", value: show(snapshot.warmConfiguredCapacity) },
          { label: "Backlog over target", value: show(snapshot.backlogBeyondCurrent) },
        ]}
      />
      <FieldList
        fields={[
          { label: "Jobs / instance", value: show(snapshot.jobsPerInstance) },
          { label: "Target workers", value: String(state.targetSize) },
          { label: "Machine", value: profile?.machineType ?? "unknown" },
        ]}
      />
      <p className="admin-empty">
        This is configured autoscaler capacity, not measured CPU, memory, or mesh-engine saturation.
      </p>
    </Panel>
  );
}
