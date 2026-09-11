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
Held at the floor regardless of queue depth.
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
                  hint="Held ready. Above zero removes the first job's cold start."
                  label="Warm floor"
                  max={maxAllowedReplicas}
                  name="minReplicas"
                />
                <NumberField
                  defaultValue={policy.maxReplicas}
                  hint={`Max ${maxAllowedReplicas}.`}
                  label="Ceiling"
                  max={maxAllowedReplicas}
                  name="maxReplicas"
                />
                <NumberField
                  defaultValue={policy.cooldownSeconds}
                  hint="Grace before a new instance counts."
                  label="Cooldown (s)"
                  name="cooldownSeconds"
                />
                <NumberField
                  defaultValue={policy.jobsPerInstance}
                  hint="Lower scales out sooner."
                  label="Jobs / instance"
                  name="jobsPerInstance"
                />
                <NumberField
                  defaultValue={policy.scaleInMaxReplicas}
                  hint="Max removed per window. No drain contract yet."
                  label="Scale-in max"
                  max={maxAllowedReplicas}
                  name="scaleInMaxReplicas"
                />
                <NumberField
                  defaultValue={policy.scaleInWindowSeconds}
                  hint="Window for that limit."
                  label="Scale-in window (s)"
                  name="scaleInWindowSeconds"
                />
              </div>
              <p className="admin-empty">Blank leaves a value unchanged. These knobs survive deploys.</p>
            </ActionForm>

            <ActionForm operation="resize" submitLabel="Resize now">
              <div className="admin-field-grid">
                <NumberField
                  defaultValue={state.targetSize}
                  hint="Temporary. The autoscaler reclaims it after cooldown."
                  label="Target size now"
                  max={maxAllowedReplicas}
                  name="size"
                />
              </div>
            </ActionForm>
          </div>
        </>
      ) : (
        <EmptyState note="No autoscaler: size is fixed at its target." />
      )}
    </Panel>
  );
}
