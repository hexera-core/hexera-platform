import assert from "node:assert/strict";
import test from "node:test";

import { readAdminTargets } from "./config";

const BASE = {
  DEPLOYMENT_ID: "hexera-dev",
  GCP_PROJECT_ID: "hexera-dev",
  GCP_REGION: "us-central1",
};

test("reads the fleet target when the deployment declares one", () => {
  const targets = readAdminTargets({
    ...BASE,
    QUEUE_NAME: "simulation_jobs",
    WORKER_MIG: "hexera-dev-workers",
    WORKER_MIG_ZONE: "us-central1-a",
  });

  assert.deepEqual(targets.fleet, {
    deploymentId: "hexera-dev",
    migName: "hexera-dev-workers",
    migZone: "us-central1-a",
    projectId: "hexera-dev",
    queueName: "simulation_jobs",
  });
});

test("a deployment with no fleet is absent, not an error", () => {
  // create-worker-fleet.sh skips the fleet entirely when WORKER_MIG is unset. The console must
  // render that as "no fleet in this deployment" rather than failing to load.
  assert.equal(readAdminTargets(BASE).fleet, null);
});

test("a fleet declared without its zone is absent", () => {
  // validate-config.sh refuses this combination at deploy time; the console must not then behave
  // as though a zonal group existed.
  assert.equal(readAdminTargets({ ...BASE, WORKER_MIG: "hexera-dev-workers" }).fleet, null);
});

test("the queue name defaults to the one create-worker-fleet.sh defaults to", () => {
  const targets = readAdminTargets({
    ...BASE,
    WORKER_MIG: "hexera-dev-workers",
    WORKER_MIG_ZONE: "us-central1-a",
  });

  assert.equal(targets.fleet?.queueName, "simulation_jobs");
});

test("refuses to build targets without a project", () => {
  assert.throws(() => readAdminTargets({ GCP_REGION: "us-central1" }), /GCP_PROJECT_ID/);
});

test("refuses to build targets without a region", () => {
  assert.throws(() => readAdminTargets({ GCP_PROJECT_ID: "hexera-dev" }), /GCP_REGION/);
});

test("Cloud Run services this deployment does not run are absent", () => {
  const targets = readAdminTargets(BASE);
  assert.equal(targets.apiService, null);
  assert.equal(targets.consoleService, null);
});

test("the admin service names itself, so its own floor is controllable", () => {
  const targets = readAdminTargets({ ...BASE, CLOUDRUN_ADMIN_SERVICE: "hexera-dev-admin" });
  assert.equal(targets.adminService, "hexera-dev-admin");
});

test("the billing export is absent unless the deployment names its table", () => {
  // A BigQuery billing export cannot be backfilled, so most deployments have none. The Costs page
  // renders that absence as a sentence naming the setting, not as an empty chart.
  assert.equal(readAdminTargets(BASE).billingExportTable, null);
  assert.equal(
    readAdminTargets({ ...BASE, BILLING_EXPORT_TABLE: "acct.export.gcp_billing_export_v1_ABC" })
      .billingExportTable,
    "acct.export.gcp_billing_export_v1_ABC",
  );
});

test("carries the project number, which only the write path needs", () => {
  assert.equal(readAdminTargets(BASE).projectNumber, null);
  assert.equal(readAdminTargets({ ...BASE, GCP_PROJECT_NUMBER: "123456789" }).projectNumber, "123456789");
});

test("the maximum replica ceiling an operator may request is bounded", () => {
  // The ceiling is the cost ceiling. A typo must not be able to request fifty e2-standard-4 VMs.
  assert.equal(readAdminTargets(BASE).maxAllowedReplicas, 12);
});

test("the ceiling cap can be raised by the deployment, but never removed", () => {
  assert.equal(readAdminTargets({ ...BASE, ADMIN_MAX_ALLOWED_REPLICAS: "30" }).maxAllowedReplicas, 30);
  assert.equal(readAdminTargets({ ...BASE, ADMIN_MAX_ALLOWED_REPLICAS: "0" }).maxAllowedReplicas, 12);
  assert.equal(readAdminTargets({ ...BASE, ADMIN_MAX_ALLOWED_REPLICAS: "nonsense" }).maxAllowedReplicas, 12);
});
