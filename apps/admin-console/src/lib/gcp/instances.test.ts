import assert from "node:assert/strict";
import test from "node:test";

import { listFleetInstances } from "./instances";

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
    instance:
      "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-a1b2",
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
