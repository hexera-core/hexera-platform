import assert from "node:assert/strict";
import test from "node:test";

import { listFleetInstances, summariseFleet } from "./instances";

const TARGET = {
  deploymentId: "hexera-dev",
  migName: "hexera-dev-workers",
  migZone: "us-central1-a",
  projectId: "hexera-dev",
  queueName: "simulation_jobs",
};

const INSTANCES = [
  {
    currentAction: "NONE",
    id: "7770850624773439803",
    instance:
      "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-a1b2",
    instanceHealth: [{ detailedHealthState: "HEALTHY" }],
    instanceStatus: "RUNNING",
    version: { instanceTemplate: "projects/p/global/instanceTemplates/tpl-old" },
  },
  {
    currentAction: "CREATING",
    instance:
      "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-c3d4",
    instanceStatus: "PROVISIONING",
    lastAttempt: { errors: { errors: [{ code: "QUOTA_EXCEEDED", message: "CPUS quota exceeded" }] } },
    version: { instanceTemplate: "projects/p/global/instanceTemplates/tpl-new" },
  },
];

function clients(instances: unknown[]) {
  return {
    instanceGroupManagers: { listManagedInstances: async () => [instances, null, {}] as never },
  } as never;
}

test("names each instance and where it is", async () => {
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);

  assert.equal(rows.length, 2);
  assert.equal(rows[0].name, "hexera-dev-workers-a1b2");
  assert.equal(rows[0].zone, "us-central1-a");
  assert.equal(rows[0].status, "RUNNING");
  assert.equal(rows[0].currentAction, "NONE");
});

test("reports the template version each instance is on", async () => {
  // Two template names in one table is how a half-finished roll becomes legible. Nothing else
  // says whether a rotation is still in flight.
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);

  assert.deepEqual(
    rows.map((row) => row.templateName),
    ["tpl-old", "tpl-new"],
  );
});

test("surfaces why an instance failed to come up", async () => {
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);
  assert.deepEqual(rows[1].lastErrors, ["QUOTA_EXCEEDED: CPUS quota exceeded"]);
});

test("an instance with no errors reports none", async () => {
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);
  assert.deepEqual(rows[0].lastErrors, []);
});

test("an empty fleet is an empty list, not a failure", async () => {
  assert.deepEqual(await listFleetInstances(clients([]), TARGET), []);
});

test("rows are ordered by name so the table does not reshuffle between reads", async () => {
  const shuffled = [INSTANCES[1], INSTANCES[0]];
  const rows = await listFleetInstances(clients(shuffled), TARGET);

  assert.deepEqual(
    rows.map((row) => row.name),
    ["hexera-dev-workers-a1b2", "hexera-dev-workers-c3d4"],
  );
});

test("carries the numeric id, because metrics are keyed by it and not by name", async () => {
  // Every per-instance Monitoring series is labelled `instance_id`. Without this the CPU and
  // memory of a worker cannot be joined to the row that names it.
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);
  assert.equal(rows[0].id, "7770850624773439803");
});

test("reports detailed health when a health check exists, and null when none does", async () => {
  // Absent health checking is not the same as unhealthy, and must not render as it.
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);
  assert.equal(rows[0].health, "HEALTHY");
  assert.equal(rows[1].health, null);
});

const ROW = {
  currentAction: "NONE",
  health: null as string | null,
  id: "1",
  lastErrors: [] as string[],
  name: "w1",
  status: "RUNNING",
  templateName: "tpl-a",
  zone: "us-central1-a",
};

test("summarises what the fleet is doing", () => {
  const summary = summariseFleet([
    { ...ROW, name: "w1" },
    { ...ROW, currentAction: "CREATING", name: "w2", status: "PROVISIONING" },
    { ...ROW, name: "w3" },
  ]);

  assert.equal(summary.total, 3);
  assert.deepEqual(summary.byStatus, [
    { count: 2, label: "RUNNING" },
    { count: 1, label: "PROVISIONING" },
  ]);
  // NONE is the resting state and would drown out the actions worth seeing.
  assert.deepEqual(summary.byAction, [{ count: 1, label: "CREATING" }]);
});

test("more than one template version means a rotation is in flight", () => {
  assert.equal(summariseFleet([ROW, { ...ROW, name: "w2" }]).rotating, false);
  const mixed = summariseFleet([ROW, { ...ROW, name: "w2", templateName: "tpl-b" }]);
  assert.equal(mixed.rotating, true);
  assert.deepEqual(mixed.templateVersions, ["tpl-a", "tpl-b"]);
});

test("no health check is unknown, not unhealthy", () => {
  // A group without health checking reports null for every instance. Counting those as unhealthy
  // would show a permanently alarming number that means nothing.
  assert.equal(summariseFleet([ROW, { ...ROW, name: "w2" }]).unhealthy, 0);
  assert.equal(summariseFleet([{ ...ROW, health: "HEALTHY" }]).unhealthy, 0);
  assert.equal(summariseFleet([{ ...ROW, health: "UNHEALTHY" }]).unhealthy, 1);
});

test("an empty fleet summarises to zeroes rather than throwing", () => {
  const summary = summariseFleet([]);
  assert.equal(summary.total, 0);
  assert.equal(summary.rotating, false);
  assert.deepEqual(summary.byStatus, []);
});
