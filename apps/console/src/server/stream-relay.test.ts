import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import http from "node:http";
import type { AddressInfo } from "node:net";
import type { Duplex } from "node:stream";
import test from "node:test";

import { attachStreamRelay, isStreamUpgrade, upstreamHeaders, upstreamUrl } from "./stream-relay";

const JOB = "0f760f95-1111-4222-8333-444455556666";
const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

type Seen = { url?: string; headers?: http.IncomingHttpHeaders };

/** A stand-in for the product API's socket: ticket "good" is accepted and greeted with one text
 *  frame; any other ticket is refused before the upgrade, as uvicorn answers a close-before-accept. */
async function fakeApi(seen: Seen) {
  const server = http.createServer((_req, res) => { res.statusCode = 404; res.end(); });
  server.on("upgrade", (req, socket: Duplex) => {
    seen.url = req.url;
    seen.headers = req.headers;
    const ticket = new URL(req.url || "/", "http://x").searchParams.get("ticket");
    if (ticket !== "good") {
      socket.end("HTTP/1.1 403 Forbidden\r\ncontent-type: text/plain\r\ncontent-length: 9\r\n\r\nForbidden");
      return;
    }
    const accept = createHash("sha1").update(`${req.headers["sec-websocket-key"]}${GUID}`).digest("base64");
    socket.write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
      + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const payload = Buffer.from('{"type":"stage","seq":1}');
    socket.write(Buffer.concat([Buffer.from([0x81, payload.length]), payload]));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

/** A stand-in for Next: every plain request is a page, every upgrade it is handed is dropped. */
async function fakeConsole(apiBase: string, nextUpgrades: string[]) {
  const server = attachStreamRelay(http.createServer((_req, res) => res.end("page")), () => apiBase);
  server.on("upgrade", (req, socket: Duplex) => { nextUpgrades.push(req.url || ""); socket.end(); });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

/** Close a test server now, open sockets included: a relayed socket or a kept-alive fetch would
 *  otherwise hold close() open and the run would never end. */
function shut(...servers: http.Server[]) {
  for (const server of servers) {
    server.closeAllConnections();
    server.close();
  }
}

function port(server: http.Server) {
  return (server.address() as AddressInfo).port;
}

type Outcome = { kind: "upgrade"; status: number; firstFrame: string } | { kind: "response"; status: number; body: string }
  | { kind: "closed" };

function dial(consolePort: number, path: string, extraHeaders: Record<string, string> = {}): Promise<Outcome> {
  return new Promise((resolve, reject) => {
    const req = http.request({
      headers: {
        connection: "Upgrade",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
        "sec-websocket-version": "13",
        upgrade: "websocket",
        ...extraHeaders,
      },
      host: "127.0.0.1",
      path,
      port: consolePort,
    });
    req.on("upgrade", (res, socket, head: Buffer) => {
      // The first frame can arrive in the same packet as the 101, and is then `head`.
      const done = (chunk: Buffer) => {
        resolve({ firstFrame: chunk.subarray(2).toString(), kind: "upgrade", status: res.statusCode || 0 });
        socket.destroy();
      };
      if (head.length) done(head);
      else socket.once("data", done);
    });
    req.on("response", (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => resolve({ body, kind: "response", status: res.statusCode || 0 }));
    });
    req.on("error", (err: NodeJS.ErrnoException) => (err.code === "ECONNRESET" ? resolve({ kind: "closed" }) : reject(err)));
    req.end();
  });
}

test("only the job stream is relayed; every other upgrade stays with Next", () => {
  const ws = { upgrade: "websocket" };
  assert.equal(isStreamUpgrade({ headers: ws, url: `/api/v1/ws/${JOB}/stream?ticket=t&since=0` }), true);
  assert.equal(isStreamUpgrade({ headers: { upgrade: "WebSocket" }, url: `/api/v1/ws/${JOB}/stream` }), true);
  assert.equal(isStreamUpgrade({ headers: ws, url: "/_next/webpack-hmr" }), false);
  assert.equal(isStreamUpgrade({ headers: ws, url: "/api/v1/ws/not-a-job/stream" }), false);
  assert.equal(isStreamUpgrade({ headers: ws, url: `/api/v1/ws/${JOB}/stream/extra` }), false);
  assert.equal(isStreamUpgrade({ headers: {}, url: `/api/v1/ws/${JOB}/stream` }), false);
});

test("the API is asked for the same path and query, at its own origin", () => {
  const target = upstreamUrl(`/api/v1/ws/${JOB}/stream?ticket=abc&since=12`, "https://dev-api.example.run.app/");
  assert.equal(target.href, `https://dev-api.example.run.app/api/v1/ws/${JOB}/stream?ticket=abc&since=12`);
});

test("no console credential and no caller-supplied identity header reaches the API", () => {
  const headers = upstreamHeaders({
    cookie: "authjs.session-token=private",
    "sec-websocket-key": "k",
    "sec-websocket-version": "13",
    "x-api-key": "smuggled",
    "x-user-id": "someone-else@example.com",
    "x-user-sig": "forged",
  }, new URL("https://api.example"), "10.0.0.1");
  assert.equal(headers.cookie, undefined);
  assert.equal(headers["x-api-key"], undefined);
  assert.equal(headers["x-user-id"], undefined);
  assert.equal(headers["x-user-sig"], undefined);
  assert.equal(headers.host, "api.example");
  assert.equal(headers["sec-websocket-key"], "k");
  assert.equal(headers["x-forwarded-for"], "10.0.0.1");
});

test("a good ticket joins the browser to the API's socket through the console", async (t) => {
  const seen: Seen = {};
  const api = await fakeApi(seen);
  const nextUpgrades: string[] = [];
  const consoleServer = await fakeConsole(`http://127.0.0.1:${port(api)}`, nextUpgrades);
  t.after(() => shut(api, consoleServer));

  const out = await dial(port(consoleServer), `/api/v1/ws/${JOB}/stream?ticket=good&since=3`,
    { cookie: "authjs.session-token=private" });

  assert.deepEqual(out, { firstFrame: '{"type":"stage","seq":1}', kind: "upgrade", status: 101 });
  assert.equal(seen.url, `/api/v1/ws/${JOB}/stream?ticket=good&since=3`);
  assert.equal(seen.headers?.cookie, undefined);
  assert.deepEqual(nextUpgrades, []);
});

test("a bad ticket reaches the browser as the API's refusal, not as a dropped connection", async (t) => {
  const api = await fakeApi({});
  const consoleServer = await fakeConsole(`http://127.0.0.1:${port(api)}`, []);
  t.after(() => shut(api, consoleServer));

  const out = await dial(port(consoleServer), `/api/v1/ws/${JOB}/stream?ticket=x&since=0`);

  assert.deepEqual(out, { body: "Forbidden", kind: "response", status: 403 });
});

test("an API that cannot be reached is a 502 with a reason, and an unset origin a 503", async (t) => {
  const closed = http.createServer();
  await new Promise<void>((resolve) => closed.listen(0, "127.0.0.1", resolve));
  const deadPort = port(closed);
  closed.close();
  const unreachable = await fakeConsole(`http://127.0.0.1:${deadPort}`, []);
  const unset = await fakeConsole("", []);
  t.after(() => shut(unreachable, unset));

  const a = await dial(port(unreachable), `/api/v1/ws/${JOB}/stream?ticket=x`);
  const b = await dial(port(unset), `/api/v1/ws/${JOB}/stream?ticket=x`);

  assert.equal(a.kind === "response" && a.status, 502);
  assert.equal(b.kind === "response" && b.status, 503);
  assert.match(b.kind === "response" ? b.body : "", /HEXERA_API_BASE_URL/);
});

test("other upgrades and plain requests still go to Next", async (t) => {
  const nextUpgrades: string[] = [];
  const consoleServer = await fakeConsole("http://127.0.0.1:9", nextUpgrades);
  t.after(() => shut(consoleServer));

  const out = await dial(port(consoleServer), "/_next/webpack-hmr");
  const page = await fetch(`http://127.0.0.1:${port(consoleServer)}/runs/new`).then((r) => r.text());

  assert.equal(out.kind, "closed");
  assert.deepEqual(nextUpgrades, ["/_next/webpack-hmr"]);
  assert.equal(page, "page");
});
