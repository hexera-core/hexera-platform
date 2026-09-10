import assert from "node:assert/strict";
import test from "node:test";

import { readServiceScaling } from "./run";

const SERVICE = {
  latestReadyRevision:
    "projects/p/locations/us-central1/services/hexera-dev-api/revisions/hexera-dev-api-00042-abc",
  name: "projects/p/locations/us-central1/services/hexera-dev-api",
  template: {
    containers: [{ image: "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee" }],
    scaling: { maxInstanceCount: 5, minInstanceCount: 1 },
  },
};

function client(service: unknown = SERVICE) {
  return { getService: async () => [service] as never } as never;
}

test("addresses the service by its fully qualified name", async () => {
  let seen: { name?: string } = {};
  const spy = {
    getService: async (request: { name?: string }) => {
      seen = request;
      return [SERVICE] as never;
    },
  } as never;

  await readServiceScaling(spy, { projectId: "p", region: "us-central1", service: "hexera-dev-api" });
  assert.equal(seen.name, "projects/p/locations/us-central1/services/hexera-dev-api");
});

test("reads the warm floor and the ceiling", async () => {
  const scaling = await readServiceScaling(client(), {
    projectId: "p",
    region: "us-central1",
    service: "hexera-dev-api",
  });

  assert.equal(scaling.minInstances, 1);
  assert.equal(scaling.maxInstances, 5);
  assert.equal(scaling.name, "hexera-dev-api");
  assert.equal(scaling.latestRevision, "hexera-dev-api-00042-abc");
  assert.equal(scaling.imageDigest, "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee");
});

test("an unset floor reads as zero, which is what Cloud Run means by it", async () => {
  // Cloud Run omits minInstanceCount when it is 0. Reporting null would render "unknown" for the
  // most common and most consequential state - a service that scales to zero and cold-starts.
  const scaleToZero = { ...SERVICE, template: { ...SERVICE.template, scaling: { maxInstanceCount: 3 } } };
  const scaling = await readServiceScaling(client(scaleToZero), {
    projectId: "p",
    region: "us-central1",
    service: "hexera-dev-api",
  });

  assert.equal(scaling.minInstances, 0);
  assert.equal(scaling.maxInstances, 3);
});

test("a service with no template renders unknowns rather than throwing", async () => {
  const scaling = await readServiceScaling(client({ name: "projects/p/locations/r/services/s" }), {
    projectId: "p",
    region: "us-central1",
    service: "s",
  });

  assert.equal(scaling.minInstances, 0);
  assert.equal(scaling.maxInstances, null);
  assert.equal(scaling.imageDigest, null);
});
