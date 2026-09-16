import assert from "node:assert/strict";
import test from "node:test";

import { applyFleetOperation, type FleetOperationDeps } from "./operations";

const ACTOR = { email: "operator@hexera.ai", subject: "accounts.google.com:1" };

const TARGETS = {
  adminService: "hexera-dev-admin",
  apiService: "hexera-dev-api",
  consoleService: "hexera-dev-console",
  deploymentId: "hexera-dev",
  fleet: {
    deploymentId: "hexera-dev",
    migName: "hexera-dev-workers",
    migZone: "us-central1-a",
    projectId: "hexera-dev",
    queueName: "simulation_jobs",
  },
  maxAllowedReplicas: 12,
  projectId: "hexera-dev",
  region: "us-central1",
};

const LIVE_AUTOSCALER = {
  autoscalingPolicy: {
    coolDownPeriodSec: 180,
    customMetricUtilizations: [
      { metric: "custom.googleapis.com/hexera/queue_depth", singleInstanceAssignment: 1 },
    ],
    maxNumReplicas: 5,
    minNumReplicas: 1,
  },
  name: "hexera-dev-workers",
};

function harness(overrides: Record<string, unknown> = {}) {
  const calls: { name: string; request: unknown }[] = [];
  const record = (name: string) => async (request: unknown) => {
    calls.push({ name, request });
    return [{ name: "operation-x" }] as never;
  };

  const audited: unknown[] = [];

  return {
    audited,
    calls,
    deps: {
      actor: ACTOR,
      audit: (entry: unknown) => audited.push(entry),
      clients: {
        autoscalers: {
          get: async () => [structuredClone(LIVE_AUTOSCALER)] as never,
          update: record("autoscalers.update"),
        },
        instanceGroupManagers: {
          deleteInstances: record("deleteInstances"),
          // The scaling writer reads the group to learn its autoscaler's name, which is not the
          // group's own.
          get: async () => [
            {
              name: "hexera-dev-workers",
              status: {
                autoscaler:
                  "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/autoscalers/hexera-dev-workers-9k6e",
              },
            },
          ] as never,
          recreateInstances: record("recreateInstances"),
          resize: record("resize"),
        },
      },
      runClient: {
        getService: async () =>
          [
            {
              name: "projects/hexera-dev/locations/us-central1/services/hexera-dev-api",
              template: { scaling: { maxInstanceCount: 5, minInstanceCount: 0 } },
            },
          ] as never,
        updateService: record("updateService"),
      },
      targets: TARGETS,
      ...overrides,
      // The clients are hand-built fakes shaped only where this test exercises them, so they are
      // narrowed to the dependency type rather than satisfying every method on the SDK clients.
    } as unknown as Omit<FleetOperationDeps, "form">,
  };
}

function form(fields: Record<string, string>) {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.append(key, value);
  return data;
}

test("raises the warm floor and says so", async () => {
  const h = harness();
  const result = await applyFleetOperation({ ...h.deps, form: form({ minReplicas: "3", operation: "scaling" }) });

  assert.equal(result.ok, true);
  assert.equal(h.calls[0].name, "autoscalers.update");
});

test("ignores blank fields rather than writing zero over them", async () => {
  // The scaling form posts every input. An untouched field arrives as an empty string, and
  // coercing that to 0 would set the fleet's floor to zero the first time anyone changed the
  // cooldown.
  const h = harness();
  await applyFleetOperation({
    ...h.deps,
    form: form({ cooldownSeconds: "300", maxReplicas: "", minReplicas: "", operation: "scaling" }),
  });

  const request = h.calls[0].request as { autoscalerResource: { autoscalingPolicy: Record<string, number> } };
  assert.equal(request.autoscalerResource.autoscalingPolicy.coolDownPeriodSec, 300);
  assert.equal(request.autoscalerResource.autoscalingPolicy.minNumReplicas, 1);
  assert.equal(request.autoscalerResource.autoscalingPolicy.maxNumReplicas, 5);
});

test("writes one audit line naming the verified actor and the change", async () => {
  const h = harness();
  await applyFleetOperation({ ...h.deps, form: form({ minReplicas: "3", operation: "scaling" }) });

  assert.equal(h.audited.length, 1);
  const entry = h.audited[0] as { action: string; actor: string; resource: string };
  assert.equal(entry.actor, "operator@hexera.ai");
  assert.equal(entry.action, "fleet.scaling.update");
  assert.equal(entry.resource, "hexera-dev-workers");
});

test("refuses a floor above the ceiling and mutates nothing", async () => {
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ maxReplicas: "2", minReplicas: "9", operation: "scaling" }),
  });

  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /floor/i);
  assert.equal(h.calls.length, 0);
  assert.equal(h.audited.length, 0, "a refused change is not an action to record");
});

test("resizes the group now", async () => {
  const h = harness();
  const result = await applyFleetOperation({ ...h.deps, form: form({ operation: "resize", size: "4" }) });

  assert.equal(result.ok, true);
  assert.equal(h.calls[0].name, "resize");
  assert.equal((h.calls[0].request as { size: number }).size, 4);
});

test("recreates an instance without demanding its name be typed", async () => {
  // Recreate is how a wedged worker is fixed, and it is the same disruption a rolling update
  // already causes. Delete is the one that needs the ceremony.
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ instance: "hexera-dev-workers-a1b2", operation: "instance.recreate" }),
  });

  assert.equal(result.ok, true);
  assert.equal(h.calls[0].name, "recreateInstances");
});

test("refuses to delete an instance unless its name was typed back", async () => {
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ confirm: "hexera-dev-workers-WRONG", instance: "hexera-dev-workers-a1b2", operation: "instance.delete" }),
  });

  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /does not match/i);
  assert.equal(h.calls.length, 0);
});

test("deletes an instance when the name was typed back", async () => {
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ confirm: "hexera-dev-workers-a1b2", instance: "hexera-dev-workers-a1b2", operation: "instance.delete" }),
  });

  assert.equal(result.ok, true);
  assert.equal(h.calls[0].name, "deleteInstances");
});

test("says plainly that a delete cannot see work in flight", async () => {
  // The console holds no database connection, so it cannot read job leases, and there is no
  // drain contract. The result must not imply a check that was never performed.
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ confirm: "hexera-dev-workers-a1b2", instance: "hexera-dev-workers-a1b2", operation: "instance.delete" }),
  });

  assert.match(result.message ?? "", /in flight|running job|cannot tell/i);
});

test("changes a Cloud Run service's warm floor", async () => {
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ minInstances: "1", operation: "service.scaling", service: "hexera-dev-api" }),
  });

  assert.equal(result.ok, true);
  assert.equal(h.calls[0].name, "updateService");
});

test("refuses a Cloud Run service this deployment does not declare", async () => {
  // The service name reaches a resource path. Only names this deployment states are addressable,
  // so a forged POST cannot aim the console at another service in the project.
  const h = harness();
  const result = await applyFleetOperation({
    ...h.deps,
    form: form({ minInstances: "1", operation: "service.scaling", service: "someone-elses-service" }),
  });

  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /not a service this deployment/i);
  assert.equal(h.calls.length, 0);
});

test("refuses an unknown operation", async () => {
  const h = harness();
  const result = await applyFleetOperation({ ...h.deps, form: form({ operation: "rm -rf" }) });

  assert.equal(result.ok, false);
  assert.equal(h.calls.length, 0);
});

test("refuses every fleet operation when the deployment declares no fleet", async () => {
  const h = harness({ targets: { ...TARGETS, fleet: null } });
  const result = await applyFleetOperation({ ...h.deps, form: form({ minReplicas: "2", operation: "scaling" }) });

  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /no worker fleet/i);
});
