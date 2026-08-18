// Responsibility: Hold one function per backend endpoint - every URL the browser knows.
// Boundaries: they return data and throw on failure; they render nothing.

/* One function per backend endpoint the UI calls.
 *
 * Every URL the browser knows lives here. Before this, `/api/v1/upload/step-file` was in
 * app.js, `/api/v1/chat/message` in app.js, `/api/v1/simulation/{id}` in two different files
 * with two different error behaviours, `/api/v1/ws/ticket` in stream.js and
 * `/api/v1/simulation/{id}/dispute` in both review.js and viewer.js - one of them bypassing
 * apiFetch entirely, so a 401 there was silent.
 *
 * These return DATA and throw on failure. They render nothing.
 */
import { apiFetch, headers, formHeaders } from "./client.js";

async function readError(r) {
  const body = await r.json().catch(() => ({ detail: r.statusText }));
  return new Error(body.detail || r.statusText);
}

export async function uploadGeometry(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await apiFetch("/api/v1/upload/step-file",
    { method: "POST", headers: formHeaders(), body: fd });
  if (!r.ok) throw await readError(r);
  return r.json();
}

export async function sendMessage(sessionId, content) {
  const r = await apiFetch("/api/v1/chat/message",
    { method: "POST", headers: headers(), body: JSON.stringify({ session_id: sessionId, content }) });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/** The job's current durable facts. `throwOnError` is false for the poller, which treats a
 *  transient 5xx as normal, and true for the deep link, which must know it failed. */
export async function getJob(jobId, { throwOnError = true } = {}) {
  const r = await apiFetch(`/api/v1/simulation/${jobId}`, { headers: headers() });
  if (!r.ok) {
    if (throwOnError) throw await readError(r);
    return null;
  }
  return r.json();
}

/** A short-lived, single-use WebSocket ticket, minted over authenticated HTTPS.
 *  Credentials go in HEADERS - never in a URL that a proxy or Cloud Run would log. */
export async function wsTicket(jobId) {
  try {
    const r = await apiFetch("/api/v1/ws/ticket",
      { method: "POST", headers: headers(), body: JSON.stringify({ job_id: jobId }) });
    if (!r || !r.ok) return null;
    const d = await r.json();
    return (d && d.ticket) || null;
  } catch {
    return null;
  }
}

/** Flag findings, or accept an unreviewed mesh against a stated bar. `mode` distinguishes
 *  them; both re-review the same mesh and return the new job. */
export async function disputeReview(jobId, { flags = [], comment = "", mode } = {}) {
  const body = { flags, comment: String(comment).slice(0, 2000) };
  if (mode) body.mode = mode;
  const r = await apiFetch(`/api/v1/simulation/${jobId}/dispute`,
    { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return r.json();
}

/** The delivered mesh surface - the structure the viewer renders. */
export async function getSurface(jobId) {
  const r = await fetch(`/api/v1/simulation/${jobId}/surface`, { headers: headers() });
  if (!r.ok) throw new Error(`surface ${r.status}`);
  return r.json();
}

/** Build the stream URL. Only the ticket and the cursor go in the query string. */
export function streamUrl(jobId, ticket, since) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const qs = new URLSearchParams();
  qs.set("ticket", ticket);
  qs.set("since", String(since));
  return `${proto}://${location.host}/api/v1/ws/${jobId}/stream?${qs}`;
}
