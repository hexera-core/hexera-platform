import { Panel, StatRow } from "@/app/_components/panel";
import type { ServiceScaling } from "@/lib/gcp/run";

import { ActionForm, NumberField } from "./action-form";

// The request tier's cold-start control. A Cloud Run service at a floor of zero costs nothing
// while idle and makes the next request wait for a container start; a floor of one is the same
// trade the worker fleet's warm pool makes, one tier up.
export function ServiceScalingPanel({
  maxAllowedReplicas,
  scaling,
}: {
  maxAllowedReplicas: number;
  scaling: ServiceScaling;
}) {
  return (
    <Panel heading={scaling.latestRevision ?? undefined} title={`Service: ${scaling.name}`}>
      <StatRow
        stats={[
          { label: "Warm floor", value: String(scaling.minInstances) },
          { label: "Ceiling", value: String(scaling.maxInstances ?? "—") },
          {
            label: "Cold starts",
            value: scaling.minInstances === 0 ? "every idle request" : "absorbed by the floor",
          },
        ]}
      />
      <div className="admin-controls">
        <ActionForm operation="service.scaling" submitLabel="Apply service scaling">
          <input name="service" type="hidden" value={scaling.name} />
          <div className="admin-field-grid">
            <NumberField
              defaultValue={scaling.minInstances}
              hint="Containers held ready. Zero means every request that arrives to an idle service waits for a container start."
              label="Warm floor"
              max={maxAllowedReplicas}
              name="minInstances"
            />
            <NumberField
              defaultValue={scaling.maxInstances}
              hint="Most containers Cloud Run may run at once."
              label="Ceiling"
              max={maxAllowedReplicas}
              name="maxInstances"
            />
          </div>
          <p className="admin-empty">
            Applies on the next revision. Like the fleet&rsquo;s knobs, these belong to this console:
            the deploy passes them when it first creates the service and omits them afterwards.
          </p>
        </ActionForm>
      </div>
    </Panel>
  );
}
