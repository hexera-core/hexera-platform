import assert from "node:assert/strict";
import test from "node:test";

import { workerCapacitySnapshot } from "./worker-capacity";
import type { FleetState } from "./fleet";
import type { Series } from "./metrics";

const BASE_STATE: FleetState = {
  autoscalerName: "hexera-dev-workers-9k6e",
  currentActions: {},
  health: { details: [], status: "ACTIVE" },
  isStable: true,
  policy: {
    cooldownSeconds: 180,
    jobsPerInstance: 2,
    maxReplicas: 5,
    metricType: "custom.googleapis.com/hexera/queue_depth",
    minReplicas: 1,
    scaleInMaxReplicas: null,
    scaleInWindowSeconds: null,
  },
  targetSize: 3,
  templateName: "hexera-dev-workers-tpl",
};

const QUEUE: Series[] = [
  {
    label: "",
    points: [
      { at: "2026-09-22T12:00:00.000Z", value: 4 },
      { at: "2026-09-22T12:01:00.000Z", value: 9 },
    ],
  },
];

test("derives configured session capacity from the live autoscaler policy", () => {
  const snapshot = workerCapacitySnapshot(BASE_STATE, QUEUE);

  assert.deepEqual(snapshot, {
    backlogBeyondCurrent: 3,
    currentConfiguredCapacity: 6,
    jobsPerInstance: 2,
    latestQueueDepth: 9,
    maxConfiguredCapacity: 10,
    warmConfiguredCapacity: 2,
  });
});

test("uses the summed queue series when the metric reader returns split series", () => {
  const snapshot = workerCapacitySnapshot(BASE_STATE, [
    { label: "a", points: [{ at: "2026-09-22T12:00:00.000Z", value: 2 }] },
    { label: "b", points: [{ at: "2026-09-22T12:00:00.000Z", value: 5 }] },
  ]);

  assert.equal(snapshot.latestQueueDepth, 7);
  assert.equal(snapshot.backlogBeyondCurrent, 1);
});

test("reports unknown capacity when the group has no autoscaler policy", () => {
  const snapshot = workerCapacitySnapshot({ ...BASE_STATE, policy: null }, QUEUE);

  assert.equal(snapshot.jobsPerInstance, null);
  assert.equal(snapshot.currentConfiguredCapacity, null);
  assert.equal(snapshot.maxConfiguredCapacity, null);
  assert.equal(snapshot.warmConfiguredCapacity, null);
  assert.equal(snapshot.backlogBeyondCurrent, null);
  assert.equal(snapshot.latestQueueDepth, 9);
});

test("reports unknown queue when the metric has not published", () => {
  const snapshot = workerCapacitySnapshot(BASE_STATE, []);

  assert.equal(snapshot.latestQueueDepth, null);
  assert.equal(snapshot.backlogBeyondCurrent, null);
  assert.equal(snapshot.currentConfiguredCapacity, 6);
});
