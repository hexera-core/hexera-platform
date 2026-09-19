import assert from "node:assert/strict";
import test from "node:test";

import { buildProductApiRequest, proxyProductApiRequest, relayedResponse } from "./proxy";

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

test("relays a gzipped upstream reply as the plain body fetch decoded it to", async () => {
  // The product API gzips every reply over 8 KB; fetch hands back the decoded body but keeps the
  // upstream wire headers. Forwarding them told the browser a plain body was gzip.
  const upstream = new Response(JSON.stringify({ reply: "x".repeat(9000) }), {
    headers: {
      "content-encoding": "gzip",
      "content-length": "1234",
      "content-type": "application/json",
      "x-request-id": "abc",
    },
    status: 200,
  });

  const relayed = relayedResponse(upstream);

  assert.equal(relayed.status, 200);
  assert.equal(relayed.headers.get("content-encoding"), null);
  assert.equal(relayed.headers.get("content-length"), null);
  assert.equal(relayed.headers.get("content-type"), "application/json");
  assert.equal(relayed.headers.get("x-request-id"), "abc");
  assert.equal((await relayed.json()).reply.length, 9000);
});

test("relays an upstream error status and body untouched", async () => {
  const upstream = new Response(JSON.stringify({ error: "nope" }), {
    headers: { "content-type": "application/json" },
    status: 422,
    statusText: "Unprocessable",
  });

  const relayed = relayedResponse(upstream);

  assert.equal(relayed.status, 422);
  assert.equal(relayed.statusText, "Unprocessable");
  assert.deepEqual(await relayed.json(), { error: "nope" });
});
