import assert from "node:assert/strict";
import { test } from "node:test";
import vm from "node:vm";

import { sessionScript } from "./session-script";

function run(script: string) {
  const context = vm.createContext({ localStorage: { setItem() {} } });
  vm.runInContext(script, context);
  return context as Record<string, unknown>;
}

test("hands main.js the API origin, the routing mode and the job to boot", () => {
  const globals = run(sessionScript({
    bootJobId: "0f760f95-1111-4222-8333-444455556666",
    ownerId: "someone@example.com",
    publicApiBaseUrl: "https://api.example",
  }));

  assert.equal(globals.__HEXERA_API_WS_BASE_URL__, "https://api.example");
  assert.equal(globals.__HEXERA_ROUTED__, true);
  assert.equal(globals.__HEXERA_BOOT_JOB__, "0f760f95-1111-4222-8333-444455556666");
});

test("a page with no job to boot says so, and an unset origin stays empty", () => {
  const globals = run(sessionScript({ bootJobId: null, ownerId: "x", publicApiBaseUrl: "" }));

  assert.equal(globals.__HEXERA_BOOT_JOB__, null);
  assert.equal(globals.__HEXERA_API_WS_BASE_URL__, "");
});

test("a job id from the URL cannot end the script element early", () => {
  // /runs/<id> hands the raw path segment over; inside a <script> a "</script>" in it would close
  // the element and let what follows run as markup.
  const hostile = "</script><script>alert(1)</script>";
  const script = sessionScript({ bootJobId: hostile, ownerId: "x", publicApiBaseUrl: "" });

  assert.ok(!script.includes("</script"), "the literal must not contain a closing tag");
  assert.equal(run(script).__HEXERA_BOOT_JOB__, hostile);
});
