// Responsibility: Be the one HTTP boundary every request this page makes passes through.
// Owns: the caller identity, the headers built from it, and the two failures every call shares.
// Boundaries: it carries no credential and offers no dialog to type one into.

/* THE HTTP boundary. Every request the UI makes goes through here.
 *
 * Owns: the caller identity, the headers built from it, and the two failures every call
 * shares - the network being down, and the session no longer being accepted. Callers still
 * handle their own domain errors; this stops those two from failing silently at twenty sites.
 *
 * There is no API key or user signature here and no dialog to type one into. A deployment that
 * needs those rejects the request at the edge; the page does not carry secrets.
 */
import { set as setState } from "../core/state.js";

const KEY_UID = "mg_uid";

/* Loaded on first use, not at import. Importing a module must not touch browser storage:
   it makes the module unloadable anywhere without a DOM, and it means the order modules
   happen to be imported in can change what a later one reads. */
let userId = null;

function store() {
  try { return globalThis.localStorage; } catch { return null; }
}

function load() {
  if (userId !== null) return;
  const ls = store();
  userId = (ls && ls.getItem(KEY_UID)) || "";
  setState.userId(userId);
}

/** Identity for this page load only - the `?user=` deep-link parameter. Not persisted: a
 *  shared link must not silently overwrite the reader's own saved identity. */
export function useIdentity(id) {
  load();
  userId = id || userId;
  setState.userId(userId);
}

export function headers() {
  load();
  const h = { "Content-Type": "application/json" };
  if (userId) h["X-User-Id"] = userId;
  return h;
}

/** Multipart uploads must not carry a Content-Type - the browser sets the boundary. */
export function formHeaders() {
  load();
  const h = {};
  if (userId) h["X-User-Id"] = userId;
  return h;
}

/* The two shared failures are reported through a notifier the caller installs, so this module
   never reaches into the DOM. main.js wires it to the notice region. */
let notifier = {
  offline() {}, online() {}, unauthorized() {}, authorized() {},
};
export function setNotifier(n) { notifier = { ...notifier, ...n }; }

export async function apiFetch(url, opts) {
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    notifier.offline();
    throw e;
  }
  notifier.online();
  if (r.status === 401 || r.status === 403) {
    notifier.unauthorized();
    const err = new Error("unauthorized");
    err.status = r.status;
    throw err;
  }
  notifier.authorized();
  return r;
}

/** READINESS for the header chip: whether the API can actually serve a job, not merely whether
 *  the process is alive. /readyz reports each dependency it needs (postgres, redis, object store),
 *  so a stack with a dead Redis reads "degraded" here instead of the green "online" that /health
 *  would have given it. Returns {ready, down:[names]}; the caller renders it. */
export async function health() {
  try {
    const r = await fetch("/readyz");
    let body = {};
    try { body = await r.json(); } catch { body = {}; }
    const checks = body.checks || {};
    const down = Object.keys(checks).filter((k) => checks[k] !== "ok");
    // `status` is authoritative. It reports "unknown" (with HTTP 200 and no checks) when the
    // readiness probe was never composed - which is NOT the same as ready, and must not read green.
    const status = String(body.status || (r.ok ? "unknown" : "down"));
    return { ready: r.ok && status === "ready" && down.length === 0, status, down };
  } catch {
    return { ready: false, status: "unreachable", down: [] };
  }
}
