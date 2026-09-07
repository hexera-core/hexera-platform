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
