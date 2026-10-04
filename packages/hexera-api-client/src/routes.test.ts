import assert from "node:assert/strict";
import test from "node:test";

import {
  HEXERA_API_PREFIX,
  hexeraApiRoutes,
  resolveHexeraApiUrl,
} from "./index";

test("defines only the FastAPI v1 product API surface", () => {
  assert.equal(HEXERA_API_PREFIX, "/api/v1");
  assert.equal(hexeraApiRoutes.uploadStepFile, "/api/v1/upload/step-file");
  assert.equal(hexeraApiRoutes.clientConfig, "/api/v1/client-config");
  assert.equal(hexeraApiRoutes.chatMessage, "/api/v1/chat/message");
  assert.equal(hexeraApiRoutes.wsTicket, "/api/v1/ws/ticket");
});

test("builds dynamic product API routes with encoded path segments", () => {
  assert.equal(
    hexeraApiRoutes.adminOpsOrganization("org/one"),
    "/api/v1/admin/ops/organizations/org%2Fone",
  );
  assert.equal(
    hexeraApiRoutes.adminOpsOrganizationCreditGrants("org one"),
    "/api/v1/admin/ops/organizations/org%20one/credit-grants",
  );
  assert.equal(
    hexeraApiRoutes.adminOpsOrganizations(0),
    "/api/v1/admin/ops/organizations?limit=0",
  );
  assert.equal(
    hexeraApiRoutes.chatHistory("session one"),
    "/api/v1/chat/history/session%20one",
  );
  assert.equal(
    hexeraApiRoutes.simulation("job/one"),
    "/api/v1/simulation/job%2Fone",
  );
  assert.equal(
    hexeraApiRoutes.simulationSurface("job one"),
    "/api/v1/simulation/job%20one/surface",
  );
});

test("resolves product API URLs without accepting Next app endpoints", () => {
  assert.equal(
    resolveHexeraApiUrl("/api/v1/client-config", "https://api.hexera.ai/"),
    "https://api.hexera.ai/api/v1/client-config",
  );

  assert.throws(
    () => resolveHexeraApiUrl("/api/internal/health" as never),
    /outside the Hexera product API/,
  );
});

test("builds the operator repair queue with only the filters it was given", () => {
  assert.equal(hexeraApiRoutes.adminRepairQueue(), "/api/v1/admin/repair/queue");
  assert.equal(
    hexeraApiRoutes.adminRepairQueue({ status: "awaiting_strategy" }),
    "/api/v1/admin/repair/queue?status=awaiting_strategy",
  );
  assert.equal(
    hexeraApiRoutes.adminRepairQueue({ unassigned: true, limit: 25 }),
    "/api/v1/admin/repair/queue?unassigned=true&limit=25",
  );
  // false is an absent filter, not `unassigned=false`: the API reads presence
  assert.equal(
    hexeraApiRoutes.adminRepairQueue({ unassigned: false }),
    "/api/v1/admin/repair/queue",
  );
  // an operator name is a query VALUE and must survive characters a name can contain
  assert.equal(
    hexeraApiRoutes.adminRepairQueue({ operator: "ana lopez" }),
    "/api/v1/admin/repair/queue?operator=ana+lopez",
  );
});

test("builds repair job routes with encoded path segments", () => {
  assert.equal(
    hexeraApiRoutes.adminRepairJob("job/one"),
    "/api/v1/admin/repair/jobs/job%2Fone",
  );
  assert.equal(
    hexeraApiRoutes.adminRepairJobAssign("job one"),
    "/api/v1/admin/repair/jobs/job%20one/assign",
  );
  assert.equal(
    hexeraApiRoutes.adminRepairJobDecide("job one"),
    "/api/v1/admin/repair/jobs/job%20one/decide",
  );
});
