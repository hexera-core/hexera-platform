import assert from "node:assert/strict";
import test from "node:test";

import { readJson } from "./console-fetch";

test("readJson returns the parsed body of an ok response", async () => {
  const response = new Response(JSON.stringify({ items: [1, 2] }), { status: 200 });
  assert.deepEqual(await readJson(Promise.resolve(response)), { items: [1, 2] });
});

test("readJson returns null for a non-ok response rather than throwing", async () => {
  const response = new Response("nope", { status: 503 });
  assert.equal(await readJson(Promise.resolve(response)), null);
});

test("readJson returns null when the request rejects", async () => {
  // fetch() REJECTS rather than resolving on a connection failure -- refused, DNS failure. An
  // uncaught rejection here propagates out of a server component and takes the whole page down,
  // which is the failure creditBalance() in the old console page was written to avoid.
  assert.equal(await readJson(Promise.reject(new Error("ECONNREFUSED"))), null);
});

test("readJson returns null when the body is not json", async () => {
  const response = new Response("<html>a proxy error page</html>", { status: 200 });
  assert.equal(await readJson(Promise.resolve(response)), null);
});

test("splitPath keeps a query string out of the path segments", async () => {
  // proxy.ts percent-encodes every segment and then copies the search off the REQUEST url. A
  // path handed over whole would encode "?" into "%3F" and the query would silently vanish --
  // /simulation?limit=5 would quietly return 25 rows.
  const { splitPath } = await import("./console-fetch");
  assert.deepEqual(splitPath("simulation?limit=5"), { segments: ["simulation"], search: "limit=5" });
  assert.deepEqual(splitPath("/credits/history"), {
    segments: ["credits", "history"],
    search: "",
  });
});
