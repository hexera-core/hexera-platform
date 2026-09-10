import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import type { FleetState } from "@/lib/gcp/fleet";

import { ActionForm, NumberField } from "./action-form";

// The policy the fleet runs on, and - the reason this panel is first on the page - whether the
// autoscaler can actually read the metric it scales on. An autoscaler in ERROR holds the group at
// its floor while the queue grows, and nothing else in the system says so.
//
// These knobs are the CONSOLE'S, not the deploy's. create-worker-fleet.sh sets them when it
// creates the autoscaler and never reconciles them afterwards, so a change made here survives the
// next deploy. That is the split-ownership decision, and it is stated on the page because an
// operator who believes the opposite will not trust the control.
export function ScalingPolicyPanel({
  maxAllowedReplicas,
  state,
}: {
  maxAllowedReplicas: number;
  state: FleetState;
}) {
  const { health, policy } = state;
  const unhealthy = health.status !== "ACTIVE" && health.status !== "ABSENT";

  return (
    <Panel heading={state.isStable ? "stable" : "reconciling"} title="Scaling policy">
      {unhealthy ? (
        <Alert>
          Autoscaler status <strong>{health.status}</strong>
          {health.details.length > 0
            ? `: ${health.details.map((detail) => `${detail.type} — ${detail.message}`).join("; ")}`
            : "."}{" "}
          While this persists the group holds at its floor regardless of queue depth.
        </Alert>
      ) : null}

      {policy ? (
        <>
          <StatRow
            stats={[
              { label: "Warm floor", value: String(policy.minReplicas ?? "—") },
              { label: "Ceiling", value: String(policy.maxReplicas ?? "—") },
              { label: "Target now", value: String(state.targetSize) },
              {
                label: "Cooldown",
                value: policy.cooldownSeconds === null ? "—" : `${policy.cooldownSeconds}s`,
              },
              { label: "Jobs / instance", value: String(policy.jobsPerInstance ?? "—") },
              {
                label: "Scale-in limit",
                value:
                  policy.scaleInMaxReplicas === null
                    ? "unset"
                    : `${policy.scaleInMaxReplicas} / ${policy.scaleInWindowSeconds ?? "?"}s`,
              },
            ]}
          />

          <div className="admin-controls">
            <ActionForm operation="scaling" submitLabel="Apply scaling policy">
              <div className="admin-field-grid">
                <NumberField
                  defaultValue={policy.minReplicas}
                  hint="Instances held ready. Above zero costs money while idle and removes the cold start from the first job."
                  label="Warm floor"
                  max={maxAllowedReplicas}
                  name="minReplicas"
                />
                <NumberField
                  defaultValue={policy.maxReplicas}
                  hint={`The cost ceiling. This deployment refuses anything above ${maxAllowedReplicas}.`}
                  label="Ceiling"
                  max={maxAllowedReplicas}
                  name="maxReplicas"
                />
                <NumberField
                  defaultValue={policy.cooldownSeconds}
                  hint="How long a new instance is given before its load counts. A worker still has to pull a multi-gigabyte image."
                  label="Cooldown (s)"
                  name="cooldownSeconds"
                />
                <NumberField
                  defaultValue={policy.jobsPerInstance}
                  hint="Queued jobs one worker is expected to carry. Lower scales out sooner."
                  label="Jobs / instance"
                  name="jobsPerInstance"
                />
                <NumberField
                  defaultValue={policy.scaleInMaxReplicas}
                  hint="Most instances the autoscaler may remove in one window. Mesh work runs for hours; there is no drain contract yet."
                  label="Scale-in max"
                  max={maxAllowedReplicas}
                  name="scaleInMaxReplicas"
                />
                <NumberField
                  defaultValue={policy.scaleInWindowSeconds}
                  hint="The window that limit applies over."
                  label="Scale-in window (s)"
                  name="scaleInWindowSeconds"
                />
              </div>
              <p className="admin-empty">
                A blank field is left as it is. These values belong to this console: the deploy sets
                them when it creates the autoscaler and does not reconcile them afterwards, so a
                change here survives the next deploy.
              </p>
            </ActionForm>

            <ActionForm operation="resize" submitLabel="Resize now">
              <div className="admin-field-grid">
                <NumberField
                  defaultValue={state.targetSize}
                  hint="Warms the pool ahead of a demo without moving the floor. The autoscaler owns the size again after its cooldown."
                  label="Target size now"
                  max={maxAllowedReplicas}
                  name="size"
                />
              </div>
            </ActionForm>
          </div>
        </>
      ) : (
        <EmptyState note="This group has no autoscaler, so its size is fixed at its target and no metric moves it. The scaling knobs below would have nothing to write to." />
      )}
    </Panel>
  );
}
