// Responsibility: Own the realtime boundary - socket lifecycle, reconnection, the replay cursor and polling.
// Owns: every timer and every socket on the page.
// Boundaries: it renders nothing and does nothing until start() is called; events go to injected callbacks.

/* THE realtime boundary: socket lifecycle, reconnection, the replay cursor, and status polling.
 *
 * It owns every timer and every socket. It does NOT own the application's startup, which is
 * what it used to do - `UI.mount()`, `checkHealth()` and the whole deep-link handler ran at
 * import time from this file, so importing the transport booted the app. Startup now lives in
 * main.js and this module does nothing until `start()` is called.
 *
 * It also renders nothing. Events go to the `onEvent` callback the caller installs; the
 * terminal result goes to `onTerminal`. That is what lets the same transport serve the live
 * page and a replay with no branching inside it.
 */
import { getJob, streamUrl, wsTicket } from "../api/endpoints.js";
import { get as getState, isTerminal, set as setState } from "../core/state.js";

const BACKOFF_MIN = 1000, BACKOFF_MAX = 30000;
const POLL_FAST_MS = 5000, POLL_SLOW_MS = 15000, POLL_SLOW_AFTER = 120;
const REPLAY_TIMEOUT_MS = 25000;

let socket = null;
let reconnectTimer = null;
let pollTimer = null;
let backoff = BACKOFF_MIN;
let cursor = 0;             // last seq RENDERED - handed back on reconnect
let pollCount = 0;
let pollFails = 0;
let sinks = { onEvent() {}, onTerminal() {}, onPollTrouble() {}, onPollRecovered() {} };

export function configure(s) { sinks = { ...sinks, ...s }; }

/** Everything this module owns, released. Safe to call when nothing is running. */
function stop() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (socket) { try { socket.close(); } catch { /* already gone */ } socket = null; }
}

/** Attach to a job. `since` 0 makes the server replay the whole log before going live, which
 *  is what a page reload during a long run needs; omitting it starts from now. */
export function start(since) {
  stop();
  setState.jobStatus("");
  pollCount = 0; pollFails = 0;
  backoff = BACKOFF_MIN;
  cursor = since || 0;
  connect();
  pollTimer = setInterval(poll, POLL_FAST_MS);
}

async function connect() {
  const jobId = getState.jobId();
  if (!jobId) return;
  const ticket = await wsTicket(jobId);
  if (!ticket) { scheduleReconnect(); return; }
  try {
    socket = new WebSocket(streamUrl(jobId, ticket, cursor));
  } catch (e) {
    console.error("WS failed:", e);
    scheduleReconnect();
    return;
  }
  socket.onopen = () => { backoff = BACKOFF_MIN; };
  socket.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (err) { console.error("bad event", err); return; }
    backoff = BACKOFF_MIN;
    // THE CURSOR. Every event carries a monotonic `seq`; we remember the last one we RENDERED
    // and hand it back on reconnect, so the server replays exactly what we missed. Without it,
    // a dropped socket punched a permanent hole in the timeline - pub/sub does not keep what
    // nobody was listening for.
    if (ev.seq) cursor = ev.seq;
    if (ev.type === "closing") setState.outcomeMessage(ev.text || "");
    sinks.onEvent(ev);
  };
  socket.onclose = scheduleReconnect;
}

/* A mesh job runs for HOURS. This used to give up after five tries in fifteen seconds - and
   because the counter only reset when a message ARRIVED, a socket that dropped during a long,
   legitimately silent meshing phase burned all five attempts against a quiet channel and then
   never tried again: the timeline died while the job ran on. Cloud Run also severs any socket
   at 60 minutes, so on a long job a disconnect is a certainty, not an edge case. Retry for as
   long as the job is alive. */
function scheduleReconnect() {
  if (isTerminal()) return;
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, backoff);
  backoff = Math.min(backoff * 2, BACKOFF_MAX);
}

/** Replay a finished run's event log, then resolve. `since=0` makes the server send the whole
 *  history and close; it renders exactly as if it had been watched live, because it is the
 *  same events through the same dispatch. */
export function replay(jobId) {
  return new Promise(async (resolve) => {
    const ticket = await wsTicket(jobId);
    if (!ticket) return resolve();
    let w;
    try { w = new WebSocket(streamUrl(jobId, ticket, 0)); } catch { return resolve(); }
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(t);
      try { w.close(); } catch { /* already closed */ }
      resolve();
    };
    const t = setTimeout(finish, REPLAY_TIMEOUT_MS);   // never wait on a dead socket
    w.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { console.error("bad event", err); return; }
      if (ev.type === "closing") {
        setState.outcomeMessage(ev.text || getState.outcomeMessage());
        finish();
        return;
      }
      sinks.onEvent(ev);
    };
    w.onclose = finish;
    w.onerror = finish;
  });
}

/* A full-aircraft snappy run legitimately takes HOURS. An old 2-hour cap made the UI silently
   stop updating mid-run, which reads as a hang. Poll until the job actually reaches a terminal
   state; back off to keep it cheap. */
async function poll() {
  const jobId = getState.jobId();
  if (!jobId) return;
  pollCount++;
  if (pollCount === POLL_SLOW_AFTER) {
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, POLL_SLOW_MS);
  }
  let job;
  try {
    job = await getJob(jobId, { throwOnError: false });
  } catch (e) {
    // the client already surfaced offline/401; on a 401 mid-run, stop hammering
    if (e && e.status === 401) { clearInterval(pollTimer); pollTimer = null; }
    return;
  }
  if (!job) {
    // a transient 5xx is normal on a busy backend; only complain if it PERSISTS, so the user
    // is not left staring at a frozen timeline wondering whether it died
    if (++pollFails >= 6) sinks.onPollTrouble();
    return;
  }
  pollFails = 0;
  sinks.onPollRecovered();
  setState.jobStatus(job.status);
  if (isTerminal()) {
    clearInterval(pollTimer); pollTimer = null;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    setTimeout(() => socket && socket.close(), 1500);
    sinks.onTerminal(job);
  }
}

/** The shape the result card is built from - derived here so the live poller and the deep
 *  link cannot disagree about what a finished job looks like. Pure. */
export function terminalResult(job, outcomeText) {
  return {
    pass: job.status === "succeeded",
    attempts: job.current_attempt || 0,
    text: outcomeText,
    files: (job.artifacts || []).map((a) => ({
      type: a.artifact_type, label: a.label, size: a.size_bytes, url: a.download_url,
    })),
    meshAvailable: !!job.mesh_available,
    verdict: job.reviewer_verdict || "",
    findings: job.reviewer_findings || [],
    reasoning: job.reviewer_reasoning || "",
  };
}
