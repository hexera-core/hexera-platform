import assert from "node:assert/strict";
import test from "node:test";

import {
  deleteFleetInstance,
  recreateFleetInstance,
  resizeFleet,
  updateScalingPolicy,
  updateServiceScaling,
} from "./fleet-writes";

const TARGET = {
  deploymentId: "hexera-dev",
  migName: "hexera-dev-workers",
  migZone: "us-central1-a",
  projectId: "hexera-dev",
  queueName: "simulation_jobs",
};

const LIMITS = { maxAllowedReplicas: 12 };

const LIVE_AUTOSCALER = {
  // The output-only fields a real GET returns alongside the policy.
  creationTimestamp: "2026-08-30T10:00:00.000-07:00",
  id: "123456789",
  recommendedSize: 3,
  selfLink: "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/autoscalers/hexera-dev-workers",
  status: "ACTIVE",
  statusDetails: [{ message: "fine", type: "OK" }],
  target: "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/instanceGroupManagers/hexera-dev-workers",
  autoscalingPolicy: {
    coolDownPeriodSec: 180,
    customMetricUtilizations: [
      {
        filter: 'resource.type = "generic_task" AND resource.labels.job = "queue-depth"',
        metric: "custom.googleapis.com/hexera/queue_depth",
        singleInstanceAssignment: 1,
        utilizationTargetType: "GAUGE",
      },
    ],
    maxNumReplicas: 5,
    minNumReplicas: 1,
  },
  name: "hexera-dev-workers",
};

function autoscalerClients(onUpdate?: (request: never) => void) {
  return {
    autoscalers: {
      get: async () => [structuredClone(LIVE_AUTOSCALER)] as never,
      update: async (request: never) => {
        onUpdate?.(request);
        return [{ name: "operation-1" }] as never;
      },
    },
  } as never;
}

test("preserves the custom metric the fleet actually scales on", async () => {
  // autoscalingPolicy is REPLACED wholesale, not merged. Sending a policy carrying only
  // minNumReplicas deletes the queue-depth utilization and silently converts the fleet to
  // CPU-based autoscaling. That failure does not error; it just scales on the wrong thing.
  let sent: { autoscalerResource?: { autoscalingPolicy?: Record<string, unknown> } } = {};
  const clients = autoscalerClients((request) => {
    sent = request;
  });

  await updateScalingPolicy(clients, TARGET, { minReplicas: 2 }, LIMITS);

  const policy = sent.autoscalerResource?.autoscalingPolicy;
  assert.deepEqual(policy?.customMetricUtilizations, LIVE_AUTOSCALER.autoscalingPolicy.customMetricUtilizations);
  assert.equal(policy?.minNumReplicas, 2);
  assert.equal(policy?.maxNumReplicas, 5, "an unspecified field keeps its live value");
  assert.equal(policy?.coolDownPeriodSec, 180);
});

test("sends back only the writable fields, and keeps the group it drives", async () => {
  // A PUT that echoed status, selfLink and recommendedSize would be sending output-only fields the
  // write does not mean to change. Omitting `target`, on the other hand, would point the
  // autoscaler at nothing.
  let sent: { autoscalerResource?: Record<string, unknown> } = {};
  await updateScalingPolicy(
    autoscalerClients((request) => {
      sent = request;
    }),
    TARGET,
    { minReplicas: 2 },
    LIMITS,
  );

  assert.deepEqual(Object.keys(sent.autoscalerResource ?? {}).sort(), [
    "autoscalingPolicy",
    "description",
    "name",
    "target",
  ]);
  assert.equal(sent.autoscalerResource?.target, LIVE_AUTOSCALER.target);
});

test("addresses the autoscaler by name and zone", async () => {
  let sent: { autoscaler?: string; project?: string; zone?: string } = {};
  await updateScalingPolicy(
    autoscalerClients((request) => {
      sent = request;
    }),
    TARGET,
    { minReplicas: 2 },
    LIMITS,
  );

  assert.equal(sent.autoscaler, "hexera-dev-workers");
  assert.equal(sent.project, "hexera-dev");
  assert.equal(sent.zone, "us-central1-a");
});

test("writes jobs-per-instance onto the existing utilization rather than a new one", async () => {
  let sent: { autoscalerResource?: { autoscalingPolicy?: { customMetricUtilizations?: { metric?: string; singleInstanceAssignment?: number }[] } } } = {};
  await updateScalingPolicy(
    autoscalerClients((request) => {
      sent = request;
    }),
    TARGET,
    { jobsPerInstance: 3 },
    LIMITS,
  );

  const utilizations = sent.autoscalerResource?.autoscalingPolicy?.customMetricUtilizations ?? [];
  assert.equal(utilizations.length, 1);
  assert.equal(utilizations[0].singleInstanceAssignment, 3);
  assert.equal(utilizations[0].metric, "custom.googleapis.com/hexera/queue_depth");
});

test("reports the policy before and after, so the audit line can name the change", async () => {
  const change = await updateScalingPolicy(autoscalerClients(), TARGET, { minReplicas: 2 }, LIMITS);
  assert.equal(change.before.minNumReplicas, 1);
  assert.equal(change.after.minNumReplicas, 2);
});

test("refuses a floor above the ceiling", async () => {
  await assert.rejects(
    () => updateScalingPolicy(autoscalerClients(), TARGET, { maxReplicas: 2, minReplicas: 4 }, LIMITS),
    /floor .* ceiling/i,
  );
});

test("refuses a floor above the LIVE ceiling when the ceiling is not being changed", async () => {
  await assert.rejects(
    () => updateScalingPolicy(autoscalerClients(), TARGET, { minReplicas: 9 }, LIMITS),
    /floor .* ceiling/i,
  );
});

test("refuses a ceiling above the deployment's cap", async () => {
  // The cap is the cost ceiling. Every replica is an e2-standard-4 running until something scales
  // it back down.
  await assert.rejects(
    () => updateScalingPolicy(autoscalerClients(), TARGET, { maxReplicas: 50 }, LIMITS),
    /12/,
  );
});

test("refuses negative and fractional values", async () => {
  await assert.rejects(() => updateScalingPolicy(autoscalerClients(), TARGET, { minReplicas: -1 }, LIMITS), /whole number/i);
  await assert.rejects(() => updateScalingPolicy(autoscalerClients(), TARGET, { maxReplicas: 2.5 }, LIMITS), /whole number/i);
  await assert.rejects(() => updateScalingPolicy(autoscalerClients(), TARGET, { cooldownSeconds: -5 }, LIMITS), /whole number/i);
});

test("refuses a resize above the cap and below zero", async () => {
  const clients = { instanceGroupManagers: { resize: async () => [{ name: "op" }] as never } } as never;
  await assert.rejects(() => resizeFleet(clients, TARGET, 50, LIMITS), /12/);
  await assert.rejects(() => resizeFleet(clients, TARGET, -1, LIMITS), /whole number/i);
});

test("resizes the group to the requested size", async () => {
  let sent: { instanceGroupManager?: string; size?: number } = {};
  const clients = {
    instanceGroupManagers: {
      resize: async (request: never) => {
        sent = request;
        return [{ name: "operation-2" }] as never;
      },
    },
  } as never;

  const result = await resizeFleet(clients, TARGET, 4, LIMITS);

  assert.equal(sent.instanceGroupManager, "hexera-dev-workers");
  assert.equal(sent.size, 4);
  assert.equal(result.operationId, "operation-2");
});

test("deletes an instance by its fully qualified URL", async () => {
  // The API rejects a bare instance name here. A partial URL is accepted and a full one is
  // unambiguous, so the full one is what is sent.
  let sent: {
    instanceGroupManagersDeleteInstancesRequestResource?: { instances?: string[] };
  } = {};
  const clients = {
    instanceGroupManagers: {
      deleteInstances: async (request: never) => {
        sent = request;
        return [{ name: "operation-3" }] as never;
      },
    },
  } as never;

  await deleteFleetInstance(clients, TARGET, "hexera-dev-workers-a1b2");

  assert.deepEqual(sent.instanceGroupManagersDeleteInstancesRequestResource?.instances, [
    "projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-a1b2",
  ]);
});

test("refuses an instance name that is not a plain Compute name", async () => {
  // The name is interpolated into a resource URL. Anything that is not a Compute instance name is
  // refused rather than escaped, because there is no legitimate value with a slash in it.
  const clients = { instanceGroupManagers: { deleteInstances: async () => [{}] as never } } as never;
  await assert.rejects(
    () => deleteFleetInstance(clients, TARGET, "../../other-zone/instances/victim"),
    /instance name/i,
  );
  await assert.rejects(() => deleteFleetInstance(clients, TARGET, ""), /instance name/i);
});

test("recreates an instance by its fully qualified URL", async () => {
  let sent: {
    instanceGroupManagersRecreateInstancesRequestResource?: { instances?: string[] };
  } = {};
  const clients = {
    instanceGroupManagers: {
      recreateInstances: async (request: never) => {
        sent = request;
        return [{ name: "operation-4" }] as never;
      },
    },
  } as never;

  await recreateFleetInstance(clients, TARGET, "hexera-dev-workers-a1b2");

  assert.deepEqual(sent.instanceGroupManagersRecreateInstancesRequestResource?.instances, [
    "projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-a1b2",
  ]);
});

test("updates Cloud Run scaling through a field mask, leaving the rest of the service alone", async () => {
  // A Service PATCH without a mask replaces the whole resource, which would drop the container,
  // its env and its service account.
  let sent: {
    service?: { name?: string; template?: { scaling?: Record<string, number> } };
    updateMask?: { paths?: string[] };
  } = {};
  const client = {
    getService: async () =>
      [
        {
          name: "projects/p/locations/us-central1/services/hexera-dev-api",
          template: { containers: [{ image: "img" }], scaling: { maxInstanceCount: 5, minInstanceCount: 0 } },
        },
      ] as never,
    updateService: async (request: never) => {
      sent = request;
      return [{ name: "run-operation" }] as never;
    },
  } as never;

  const change = await updateServiceScaling(
    client,
    { projectId: "p", region: "us-central1", service: "hexera-dev-api" },
    { minInstances: 1 },
    LIMITS,
  );

  assert.deepEqual(sent.updateMask?.paths, ["template.scaling"]);
  assert.equal(sent.service?.template?.scaling?.minInstanceCount, 1);
  assert.equal(sent.service?.template?.scaling?.maxInstanceCount, 5);
  assert.equal(change.before.minInstanceCount, 0);
  assert.equal(change.after.minInstanceCount, 1);
});

test("refuses a Cloud Run floor above its ceiling or above the cap", async () => {
  const client = {
    getService: async () =>
      [{ name: "projects/p/locations/r/services/s", template: { scaling: { maxInstanceCount: 5 } } }] as never,
    updateService: async () => [{}] as never,
  } as never;
  const args = { projectId: "p", region: "r", service: "s" };

  await assert.rejects(() => updateServiceScaling(client, args, { minInstances: 9 }, LIMITS), /floor .* ceiling/i);
  await assert.rejects(() => updateServiceScaling(client, args, { maxInstances: 40 }, LIMITS), /12/);
});
