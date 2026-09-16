import assert from "node:assert/strict";
import test from "node:test";

import { readWorkerProfile } from "./worker-profile";

const TEMPLATE = {
  name: "hexera-dev-worker-tpl-abc123-1445b8",
  properties: {
    disks: [
      {
        boot: true,
        initializeParams: {
          diskSizeGb: "100",
          diskType: "pd-balanced",
          sourceImage: "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts",
        },
      },
    ],
    machineType: "e2-standard-4",
    metadata: {
      items: [
        { key: "worker-image", value: "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee" },
        { key: "redis-url", value: "redis://10.62.0.9:6379" },
        { key: "postgres-password-secret", value: "hexera-dev-postgres-password" },
        { key: "startup-script", value: "#!/usr/bin/env bash\nexport SOMETHING=secret\n" },
      ],
    },
    networkInterfaces: [
      {
        network: "projects/p/global/networks/default",
        subnetwork: "projects/p/regions/us-central1/subnetworks/default",
      },
    ],
    serviceAccounts: [
      {
        email: "hexera-dev-worker@hexera-dev.iam.gserviceaccount.com",
        scopes: ["https://www.googleapis.com/auth/cloud-platform"],
      },
    ],
  },
};

function clients(template: unknown = TEMPLATE) {
  return { instanceTemplates: { get: async () => [template] as never } } as never;
}

test("reads the shape of a worker", async () => {
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);

  assert.equal(profile.machineType, "e2-standard-4");
  assert.equal(profile.bootDiskGb, 100);
  assert.equal(profile.bootDiskType, "pd-balanced");
  assert.equal(profile.osImage, "ubuntu-2204-lts");
  assert.equal(profile.serviceAccount, "hexera-dev-worker@hexera-dev.iam.gserviceaccount.com");
  assert.deepEqual(profile.scopes, ["https://www.googleapis.com/auth/cloud-platform"]);
  assert.equal(profile.network, "default");
  assert.equal(profile.subnetwork, "default");
});

test("shows the pinned application digest, which is the point of the panel", async () => {
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);
  assert.equal(profile.workerImage, "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee");
});

test("lists metadata keys", async () => {
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);
  assert.deepEqual(profile.metadataKeys, [
    "postgres-password-secret",
    "redis-url",
    "startup-script",
    "worker-image",
  ]);
});

test("withholds every metadata value except the allowlisted one", async () => {
  // Instance metadata is readable by anyone holding compute.instances.get, and this page is one
  // template change away from printing whatever lands there next. The allowlist is the control;
  // this test is what stops it being widened by accident.
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);
  const serialised = JSON.stringify(profile);

  assert.ok(!serialised.includes("redis://10.62.0.9:6379"));
  assert.ok(!serialised.includes("hexera-dev-postgres-password"));
  assert.ok(!serialised.includes("export SOMETHING=secret"));
});

test("a template with nothing in it renders as unknowns, not a crash", async () => {
  const profile = await readWorkerProfile(clients({ name: "bare" }), "hexera-dev", "bare");

  assert.equal(profile.machineType, null);
  assert.equal(profile.bootDiskGb, null);
  assert.deepEqual(profile.metadataKeys, []);
  assert.deepEqual(profile.scopes, []);
});

test("a non-boot disk is not mistaken for the boot disk", async () => {
  const twoDisks = {
    ...TEMPLATE,
    properties: {
      ...TEMPLATE.properties,
      disks: [
        { boot: false, initializeParams: { diskSizeGb: "500", diskType: "pd-ssd" } },
        ...TEMPLATE.properties.disks,
      ],
    },
  };
  const profile = await readWorkerProfile(clients(twoDisks), "hexera-dev", TEMPLATE.name);

  assert.equal(profile.bootDiskGb, 100);
  assert.equal(profile.bootDiskType, "pd-balanced");
});
