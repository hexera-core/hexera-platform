import assert from "node:assert/strict";
import test from "node:test";

import { buildProductApiRequest, proxyProductApiRequest } from "./proxy";

test("rejects unauthenticated requests before reaching the product API", async () => {
  await assert.rejects(
    () =>
      buildProductApiRequest({
        baseUrl: "https://api.hexera.ai",
        ownerId: null,
        request: new Request("https://console.hexera.ai/api/v1/client-config"),
        routePath: ["client-config"],
      }),
    /Authentication required/,
  );
});

test("builds product API requests with server-owned identity headers", async () => {
  const productRequest = await buildProductApiRequest({
    baseUrl: "https://api.hexera.ai/",
    ownerId: "user@example.com",
    request: new Request("https://console.hexera.ai/api/v1/chat/message?debug=1", {
      body: JSON.stringify({ content: "hello" }),
      headers: {
        "content-type": "application/json",
        cookie: "auth-cookie=private",
      },
      method: "POST",
    }),
    routePath: ["chat", "message"],
    userTokenSecret: "secret",
  });

  assert.equal(productRequest.url, "https://api.hexera.ai/api/v1/chat/message?debug=1");
  assert.equal(productRequest.method, "POST");
  assert.equal(productRequest.headers.get("x-user-id"), "user@example.com");
  assert.equal(productRequest.headers.get("x-user-sig")?.length, 64);
  assert.equal(productRequest.headers.get("cookie"), null);
  assert.equal(await productRequest.text(), JSON.stringify({ content: "hello" }));
});

test("refuses unauthenticated proxy calls before checking deployment config", async () => {
  const response = await proxyProductApiRequest(
    new Request("https://console.hexera.ai/api/v1/client-config"),
    ["client-config"],
    null,
  );

  assert.equal(response.status, 401);
});
