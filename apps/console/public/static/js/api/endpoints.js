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

/* THE DIRECT UPLOAD. A hosted deployment's front end refuses a request body over 32 MiB before the
   API sees it ("413 Request Entity Too Large"), and real CAD is often 40-500 MB. So a large file
   does not travel in a request body: the API hands out a signed PUT URL for one object, the
   browser sends the bytes straight to storage (with progress), and the API then checks what
   arrived and answers exactly what the multipart upload answers. Smaller files keep the one-request
   multipart upload they have always used. */
export const DIRECT_UPLOAD_FROM_BYTES = 20 * 1024 * 1024;

/** Upload a geometry file; resolves to {session_id, step_filename, intake_greeting}. `onProgress`
 *  hears {phase: "sending", sent, total} while a large file travels, then {phase: "checking"}. */
export async function uploadGeometry(file, { onProgress } = {}) {
  if (file.size > DIRECT_UPLOAD_FROM_BYTES) return uploadGeometryDirect(file, { onProgress });
  const fd = new FormData();
  fd.append("file", file);
  const r = await apiFetch("/api/v1/upload/step-file",
    { method: "POST", headers: formHeaders(), body: fd });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/* SEVERAL FILES OF ONE ASSEMBLY (each a CAD solid in the assembly's coordinates): one request, the
   first as `file` and the rest as `parts`; the server joins them into one file of several bodies.
   They travel in the request body, so together they stay under the direct upload's threshold. */
export const ASSEMBLY_UPLOAD_MAX_BYTES = DIRECT_UPLOAD_FROM_BYTES;

export async function uploadAssembly(files) {
  const fd = new FormData();
  files.forEach((f, i) => fd.append(i === 0 ? "file" : "parts", f));
  const r = await apiFetch("/api/v1/upload/step-file",
    { method: "POST", headers: formHeaders(), body: fd });
  if (!r.ok) throw await readError(r);
  return r.json();
}

export async function uploadGeometryDirect(file, { onProgress, makeRequest } = {}) {
  const r = await apiFetch("/api/v1/upload/direct", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ filename: file.name, size_bytes: file.size }) });
  if (!r.ok) throw await readError(r);
  const begun = await r.json();
  await putToStorage(begun, file, onProgress, { makeRequest });
  if (onProgress) onProgress({ phase: "checking" });
  const f = await apiFetch(begun.finalize_url, {
    method: "POST", headers: headers(), body: JSON.stringify({ filename: file.name }) });
  if (!f.ok) throw await readError(f);
  return f.json();
}

/** Send the file to the signed URL, reporting the browser's own upload progress. NOT apiFetch:
 *  this goes to the object store, not the API, and carries no identity header - the URL is the
 *  permission. So a 403 here is an expired or refused link, never a signed-out session. */
export function putToStorage(begun, file, onProgress, { makeRequest } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = makeRequest ? makeRequest() : new XMLHttpRequest();
    xhr.open(begun.method || "PUT", begun.upload_url);
    for (const [k, v] of Object.entries(begun.headers || {})) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (onProgress && e.lengthComputable) {
        onProgress({ phase: "sending", sent: e.loaded, total: e.total });
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(storageRefusal(xhr.status)));
    };
    xhr.onerror = () => reject(new Error("the file could not be sent to storage. Check your "
      + "connection and try again"));
    xhr.onabort = () => reject(new Error("the upload was stopped before it finished"));
    xhr.send(file);
  });
}

function storageRefusal(status) {
  if (status === 403) return "the upload link expired or was refused. Try again";
  if (status === 413) return "storage refused a file this large";
  return "storage did not accept the file (" + status + "). Try again";
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

/** Stop a run the caller owns. `reason` is optional and kept with the job. Answers the job's new
 *  durable facts; a repeat on an already-cancelled run answers the same way rather than failing. */
export async function cancelJob(jobId, reason = "") {
  const r = await apiFetch(`/api/v1/simulation/${jobId}/cancel`,
    { method: "POST", headers: headers(),
      body: JSON.stringify({ reason: String(reason || "").slice(0, 500) }) });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/** The geometry check for an upload: `status` is off | none | pending | scouted | ready |
 *  unsupported | failed; a scouted or ready check carries the proposal, a ready one the picture
 *  links, a failed one its `reason` and - when a step can be run again - the `retry` step. A
 *  step whose worker died is served as failed once its time is up. Never throws on a plain
 *  "not yet" - the composer polls it. */
export async function getGeometryCheck(sessionId) {
  const r = await apiFetch(`/api/v1/geometry/${sessionId}/check`, { headers: headers() });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/** What the user confirmed on the geometry-check picture. The reply carries the sentence the
 *  intake will read, so the conversation can show it. */
export async function confirmGeometryCheck(sessionId, body) {
  const r = await apiFetch(`/api/v1/geometry/${sessionId}/check/confirm`,
    { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/** Run a step of the geometry check again - the scout when the part was never measured, the
 *  naming when it was; `step` left out runs the one the check reports. Resolves to the check as
 *  it then stands, the same shape getGeometryCheck returns. */
export async function retryGeometryCheck(sessionId, step) {
  const r = await apiFetch(`/api/v1/geometry/${sessionId}/check/retry`,
    { method: "POST", headers: headers(), body: JSON.stringify(step ? { step } : {}) });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/** The part's skin as the viewer draws it - the same structure a delivered surface has. 404
 *  when the check stored none, which the stage treats as "show the card instead". */
export async function getGeometrySkin(sessionId) {
  const r = await apiFetch(`/api/v1/geometry/${sessionId}/check/skin`, { headers: headers() });
  if (!r.ok) throw await readError(r);
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
  const apiBase = globalThis.__HEXERA_API_WS_BASE_URL__;
  const urlBase = apiBase ? new URL(apiBase) : location;
  const proto = urlBase.protocol === "https:" ? "wss" : "ws";
  const qs = new URLSearchParams();
  qs.set("ticket", ticket);
  qs.set("since", String(since));
  return `${proto}://${urlBase.host}/api/v1/ws/${jobId}/stream?${qs}`;
}

/** A parked repair run's unsure defects, grouped and sampled: {status: open|offered|closed,
 *  geometry_session_id, groups, decided, open}. Throws on failure (409: nothing to review). */
export async function getReview(jobId) {
  const r = await apiFetch(`/api/v1/simulation/${jobId}/review`, { headers: headers() });
  if (!r.ok) throw await readError(r);
  return r.json();
}

/** Record one decision. `body` is {item_id | group_key, action, text?}. Answers {ok, status, data}
 *  and does not throw on a refusal: the caller words each one. */
export async function postReviewDecision(jobId, body) {
  const r = await apiFetch(`/api/v1/simulation/${jobId}/review/decision`,
    { method: "POST", headers: headers(), body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

/** Resume the parked run with the decisions so far. 202 on success; 503 means the dispatch
 *  failed and the review was put back as it was; 409 means the run is no longer waiting. */
export async function postReviewRun(jobId) {
  const r = await apiFetch(`/api/v1/simulation/${jobId}/review/run`,
    { method: "POST", headers: headers() });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}
