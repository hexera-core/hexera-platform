// Responsibility: Carry the browser's live job stream through the console's own origin to the product API.
// Owns: the one upgrade path the console answers (/api/v1/ws/<job>/stream) and the byte pipe behind it.
// Boundaries: it adds no credential - the API authenticates the socket by its single-use ticket - and
// every other request and upgrade is left to Next exactly as before.
//
// WHY THIS EXISTS. The run page's timeline (engine chosen, plan, builder progress) arrives over a
// WebSocket. The browser dials its own origin, and everything at that origin is the console:
// dev.console.hexera.ai is a load balancer whose only backend is the console service, and a slug
// environment's console is its own Cloud Run URL. Next's server answers HTTP only - an upgrade for
// a path an app route owns is closed with no reply (router-server.js: `if (matchedOutput) return
// socket.end()`), which Cloud Run's front end reports to the browser as 503. The socket never
// reached the API, the page minted a fresh ticket every 30 seconds for the whole run, and the user
// saw "Generating mesh" with no steps under it.
//
// Run as a preload (`node --import ./src/server/preload.ts server.js`, see the Dockerfile), it
// wraps the HTTP server Next's standalone server.js creates, so Next's own startup, signals and
// keep-alive handling stay exactly as Next ships them.

import http from "node:http";
import https from "node:https";
import { Socket } from "node:net";
import type { IncomingHttpHeaders, IncomingMessage, Server } from "node:http";
import type { Duplex } from "node:stream";

/** The one path relayed: the product API's job stream, job id a UUID. Anything else is Next's. */
const STREAM_PATH = /^\/api\/v1\/ws\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/stream$/i;

/** How long to wait for the API to answer the handshake. The live socket itself has no idle limit
 *  here: meshing can run for an hour without an event, and the platform's request timeout and the
 *  API's own session cap (WS_MAX_SESSION_SECONDS) are what end it. */
const HANDSHAKE_TIMEOUT_MS = 15_000;

/** Browser handshake headers worth carrying. Everything else stays behind - above all the cookie
 *  (the console's own session) and any x-api-key / x-user-id / x-user-sig a client might send: the
 *  API's socket falls back to those headers when no ticket is given, and this relay must never be
 *  a way to present them from the console's side. */
const FORWARDED_HEADERS = [
  "origin",
  "sec-websocket-extensions",
  "sec-websocket-key",
  "sec-websocket-protocol",
  "sec-websocket-version",
  "user-agent",
];

export function isStreamUpgrade(req: Pick<IncomingMessage, "headers" | "url">): boolean {
  const upgrade = String(req.headers.upgrade || "").toLowerCase();
  if (upgrade !== "websocket" || !req.url) return false;
  let pathname: string;
  try {
    pathname = new URL(req.url, "http://console.invalid").pathname;
  } catch {
    return false;
  }
  return STREAM_PATH.test(pathname);
}

/** The API URL for this upgrade: the API's origin, the request's own path and query. */
export function upstreamUrl(requestUrl: string, apiBaseUrl: string): URL {
  const source = new URL(requestUrl, "http://console.invalid");
  const target = new URL(source.pathname, apiBaseUrl);
  target.search = source.search;
  return target;
}

export function upstreamHeaders(source: IncomingHttpHeaders, target: URL, remoteAddress?: string) {
  const headers: Record<string, string> = {};
  for (const name of FORWARDED_HEADERS) {
    const value = source[name];
    if (typeof value === "string" && value) headers[name] = value;
  }
  headers.connection = "Upgrade";
  headers.upgrade = "websocket";
  headers.host = target.host;
  const prior = typeof source["x-forwarded-for"] === "string" ? source["x-forwarded-for"] : "";
  const chain = [prior, remoteAddress || ""].filter(Boolean).join(", ");
  if (chain) headers["x-forwarded-for"] = chain;
  return headers;
}

function statusBlock(statusCode: number, statusMessage: string, rawHeaders: string[]): string {
  let out = `HTTP/1.1 ${statusCode} ${statusMessage}\r\n`;
  for (let i = 0; i + 1 < rawHeaders.length; i += 2) out += `${rawHeaders[i]}: ${rawHeaders[i + 1]}\r\n`;
  return `${out}\r\n`;
}

/** A refusal written straight onto the raw socket, then the socket closed. */
function refuse(socket: Duplex, statusCode: number, statusMessage: string, detail: string) {
  if (socket.destroyed) return;
  const body = JSON.stringify({ detail });
  socket.end(statusBlock(statusCode, statusMessage, [
    "Content-Type", "application/json",
    "Content-Length", String(Buffer.byteLength(body)),
    "Connection", "close",
  ]) + body);
}

/** Relay one browser upgrade to the API and pipe the two sockets together. The API's answer is
 *  passed back as it is: 101 joins the pipe, and a refusal (403 for a bad or spent ticket) reaches
 *  the browser as that refusal rather than as a dropped connection. */
export function relayStream(req: IncomingMessage, socket: Duplex, head: Buffer, apiBaseUrl: string) {
  if (!apiBaseUrl) {
    refuse(socket, 503, "Service Unavailable", "HEXERA_API_BASE_URL is not configured");
    return;
  }
  let target: URL;
  try {
    target = upstreamUrl(req.url || "/", apiBaseUrl);
  } catch {
    refuse(socket, 503, "Service Unavailable", "HEXERA_API_BASE_URL is not a URL");
    return;
  }

  const transport = target.protocol === "https:" ? https : http;
  const upstream = transport.request(target, {
    headers: upstreamHeaders(req.headers, target, req.socket?.remoteAddress),
    method: "GET",
    timeout: HANDSHAKE_TIMEOUT_MS,
  });
  let answered = false;

  // The browser leaving before the API answers ends the attempt; nothing is left half-open.
  const onEarlyClose = () => { if (!answered) upstream.destroy(); };
  socket.once("close", onEarlyClose);
  socket.on("error", () => upstream.destroy());

  upstream.on("upgrade", (res, apiSocket, apiHead) => {
    answered = true;
    socket.off("close", onEarlyClose);
    if (socket.destroyed) { apiSocket.destroy(); return; }
    // A live socket is silent for as long as meshing is; no idle timer may cut it.
    upstream.setTimeout(0);
    apiSocket.setTimeout(0);
    apiSocket.setNoDelay(true);
    apiSocket.setKeepAlive(true, 30_000);
    if (socket instanceof Socket) {
      socket.setTimeout(0);
      socket.setNoDelay(true);
      // a browser that vanishes without closing (a laptop lid, a dropped network) is noticed
      socket.setKeepAlive(true, 30_000);
    }
    socket.write(statusBlock(res.statusCode || 101, res.statusMessage || "Switching Protocols", res.rawHeaders));
    if (apiHead && apiHead.length) socket.write(apiHead);
    if (head && head.length) apiSocket.write(head);
    apiSocket.on("error", () => socket.destroy());
    socket.on("error", () => apiSocket.destroy());
    apiSocket.on("close", () => socket.destroy());
    socket.on("close", () => apiSocket.destroy());
    // The API's last bytes (its close frame) are flushed to the browser before the browser's side
    // is ended. The browser hanging up ends both at once: an upgraded server socket is half-open
    // by default, and a WebSocket has no use for half a connection.
    apiSocket.pipe(socket);
    socket.pipe(apiSocket, { end: false });
    socket.on("end", () => { apiSocket.destroy(); socket.destroy(); });
  });

  upstream.on("response", (res) => {
    // NOT AN UPGRADE: the API refused the handshake (an invalid or spent ticket is a 403). Hand
    // that answer back whole, so the browser and the deploy check see the API's own refusal.
    answered = true;
    socket.off("close", onEarlyClose);
    if (socket.destroyed) { res.destroy(); return; }
    const kept: string[] = [];
    for (let i = 0; i + 1 < res.rawHeaders.length; i += 2) {
      const name = res.rawHeaders[i].toLowerCase();
      // Node has already undone any chunking; the bytes below are the body itself.
      if (name === "transfer-encoding" || name === "connection" || name === "keep-alive") continue;
      kept.push(res.rawHeaders[i], res.rawHeaders[i + 1]);
    }
    kept.push("Connection", "close");
    socket.write(statusBlock(res.statusCode || 502, res.statusMessage || "", kept));
    res.pipe(socket);
    res.on("error", () => socket.destroy());
  });

  upstream.on("timeout", () => {
    upstream.destroy();
    if (!answered) {
      answered = true;
      refuse(socket, 504, "Gateway Timeout", "the API did not answer the stream handshake in time");
    }
  });
  upstream.on("error", () => {
    if (!answered) {
      answered = true;
      refuse(socket, 502, "Bad Gateway", "the API could not be reached for the live stream");
    }
  });
  upstream.end();
}

/** Route stream upgrades on `server` to the relay; every other event goes where it went before.
 *  The API origin is read per upgrade, so the value Next loads from .env files in development is
 *  the one used, exactly as the HTTP proxy route reads it. */
export function attachStreamRelay(
  server: Server,
  apiBaseUrl: () => string = () => process.env.HEXERA_API_BASE_URL || "",
) {
  const emit = server.emit;
  server.emit = function relayingEmit(this: Server, event: string | symbol, ...args: unknown[]) {
    if (event === "upgrade") {
      const [req, socket, head] = args as [IncomingMessage, Duplex, Buffer];
      if (isStreamUpgrade(req)) {
        relayStream(req, socket, head, apiBaseUrl());
        return true;
      }
    }
    return emit.call(this, event, ...args);
  } as Server["emit"];
  return server;
}

let installed = false;

/** Wrap every HTTP server created from here on. Next's standalone server.js creates exactly one,
 *  through `http.createServer`, after this preload has run. */
export function installStreamRelay() {
  if (installed) return;
  installed = true;
  const create = http.createServer;
  http.createServer = function createServerWithRelay(...args: unknown[]) {
    return attachStreamRelay((create as (...a: unknown[]) => Server).apply(http, args));
  } as typeof http.createServer;
}
