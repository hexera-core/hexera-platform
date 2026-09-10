import assert from "node:assert/strict";
import test from "node:test";

import { readFleetState } from "./fleet";

const TARGET = {
  deploymentId: "hexera-dev",
  migName: "hexera-dev-workers",
  migZone: "us-central1-a",
  projectId: "hexera-dev",
  queueName: "simulation_jobs",
};

const MANAGER = {
  currentActions: { creating: 2, deleting: 0, none: 3, recreating: 0, verifying: 1 },
  instanceTemplate:
    "https://www.googleapis.com/compute/v1/projects/hexera-dev/global/instanceTemplates/hexera-dev-worker-tpl-abc123-1445b8",
  name: "hexera-dev-workers",
  status: { isStable: false },
  targetSize: 5,
};

const AUTOSCALER = {
  autoscalingPolicy: {
    coolDownPeriodSec: 180,
    customMetricUtilizations: [
      {
        filter: 'resource.type = "generic_task"',
        metric: "custom.googleapis.com/hexera/queue_depth",
        singleInstanceAssignment: 1,
      },
    ],
    maxNumReplicas: 5,
    minNumReplicas: 1,
  },
  name: "hexera-dev-workers",
  status: "ACTIVE",
  statusDetails: [],
};

function clients(manager: unknown, autoscaler: unknown) {
  return {
    autoscalers: {
      get: async () => {
        if (autoscaler instanceof Error) throw autoscaler;
        return [autoscaler] as never;
      },
    },
    instanceGroupManagers: { get: async () => [manager] as never },
  } as never;
}

function notFound() {
  return Object.assign(new Error("The resource was not found"), { code: 5 });
}

test("reads the group's size, actions and stability", async () => {
  const state = await readFleetState(clients(MANAGER, AUTOSCALER), TARGET);

  assert.equal(state.targetSize, 5);
  assert.equal(state.isStable, false);
  assert.equal(state.currentActions.creating, 2);
  assert.equal(state.templateName, "hexera-dev-worker-tpl-abc123-1445b8");
});

test("reads the scaling policy the fleet actually runs on", async () => {
  const state = await readFleetState(clients(MANAGER, AUTOSCALER), TARGET);

  assert.deepEqual(state.policy, {
    cooldownSeconds: 180,
    jobsPerInstance: 1,
    maxReplicas: 5,
    metricType: "custom.googleapis.com/hexera/queue_depth",
    minReplicas: 1,
    scaleInMaxReplicas: null,
    scaleInWindowSeconds: null,
  });
});

test("reads a scale-in control when one is set", async () => {
  const guarded = {
    ...AUTOSCALER,
    autoscalingPolicy: {
      ...AUTOSCALER.autoscalingPolicy,
      scaleInControl: { maxScaledInReplicas: { fixed: 1 }, timeWindowSec: 600 },
    },
  };
  const state = await readFleetState(clients(MANAGER, guarded), TARGET);

  assert.equal(state.policy?.scaleInMaxReplicas, 1);
  assert.equal(state.policy?.scaleInWindowSeconds, 600);
});

test("surfaces an autoscaler that cannot read its metric", async () => {
  // This is the failure the panel exists for: the fleet sits silently at its floor while the
  // queue grows, and nothing else in the system says so.
  const broken = {
    ...AUTOSCALER,
    status: "ERROR",
    statusDetails: [{ message: "The custom metric is invalid", type: "CUSTOM_METRIC_INVALID" }],
  };
  const state = await readFleetState(clients(MANAGER, broken), TARGET);

  assert.equal(state.health.status, "ERROR");
  assert.deepEqual(state.health.details, [
    { message: "The custom metric is invalid", type: "CUSTOM_METRIC_INVALID" },
  ]);
});

test("a group with no autoscaler reports no policy rather than failing", async () => {
  const state = await readFleetState(clients(MANAGER, notFound()), TARGET);

  assert.equal(state.policy, null);
  assert.equal(state.health.status, "ABSENT");
  assert.equal(state.targetSize, 5);
});

test("an error that is not NOT_FOUND is not swallowed", async () => {
  // A permission failure must reach the page as an error. Treating it as "no autoscaler" would
  // render a fleet with no scaling policy, which is a different and untrue statement.
  const denied = Object.assign(new Error("Permission denied"), { code: 7 });
  await assert.rejects(() => readFleetState(clients(MANAGER, denied), TARGET), /Permission denied/);
});

test("missing counters read as zero, not undefined", async () => {
  const bare = { ...MANAGER, currentActions: {} };
  const state = await readFleetState(clients(bare, AUTOSCALER), TARGET);

  assert.equal(state.currentActions.deleting, 0);
  assert.equal(state.currentActions.none, 0);
});
