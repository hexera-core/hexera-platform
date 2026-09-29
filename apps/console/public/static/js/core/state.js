// Responsibility: Hold the page's mutable values, each with exactly one owner.
// Owns: what terminal means, so the poller, the reconnect scheduler and the composer cannot disagree.
// Boundaries: data only - no DOM, no network, no timers, so a transition can be tested by calling it.

/* The application's mutable values, each with exactly one owner.
 *
 * These used to be top-level `let`s spread across four files and reassigned from a fifth:
 * `jobId` was declared in app.js and rewritten by review.js and stream.js; `_outcomeMsg` was
 * declared in stream.js, read by viewer.js and rewritten by review.js; `_liveCur` was declared
 * in ui.js and rewritten by review.js. Nothing could be reasoned about locally.
 *
 * This is a small explicit store, not a state framework: a plain object and named setters.
 * Everything here is data - no DOM, no network, no timers - so a state transition can be
 * tested by calling it.
 */

const state = {
  sessionId: null,      // set by upload, required by chat
  jobId: null,          // the run this page is attached to
  runSessionId: null,   // the conversation that run came from; null for a run opened by link
  jobStatus: "",        // last status the poller saw: '' | running | succeeded | failed | cancelled
  outcomeMessage: "",   // the closing text, held until the result card is built
  laneCursor: null,     // which timeline lane the event stream is currently in
  userId: "",           // identity for this page load (settings or ?user=)
  runOrigins: {},       // job id -> the conversation it came from, for every run this page began
};

export const get = {
  sessionId: () => state.sessionId,
  jobId: () => state.jobId,
  runSessionId: () => state.runSessionId,
  /** The conversation a run this page began came from; null for one opened by link or unknown. */
  runOrigin: (jobId) => (jobId && state.runOrigins[jobId]) || null,
  jobStatus: () => state.jobStatus,
  outcomeMessage: () => state.outcomeMessage,
  laneCursor: () => state.laneCursor,
  userId: () => state.userId,
};

export const set = {
  sessionId(v) { state.sessionId = v || null; },
  jobId(v) { state.jobId = v || null; },
  jobStatus(v) { state.jobStatus = v || ""; },
  outcomeMessage(v) { state.outcomeMessage = v || ""; },
  laneCursor(v) { state.laneCursor = v || null; },
  userId(v) { state.userId = v || ""; },
};

/** The statuses a run cannot leave. One exported set, so the deep link, the poller and the
 *  reconnect scheduler cannot disagree about what "finished" means. */
export const TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled"]);
export function isTerminalStatus(status) { return TERMINAL_STATUSES.has(status); }

/** True once the run reached a terminal state - the single place that decides it, so the
 *  poller, the reconnect scheduler and the composer cannot disagree. */
export function isTerminal() {
  return isTerminalStatus(state.jobStatus);
}

/** What the run header says while NO WORKER HAS THE JOB. The fleet scales to zero, so a fresh
 *  job can sit `pending` for minutes while a VM boots; the backend puts its estimate on the
 *  status (`worker_wake_minutes`, only in that state). Pure: (job) -> text, "" once a worker has
 *  started or the job is past waiting. Nothing is invented - no estimate on the wire means the
 *  line names the wait and no number. */
export function waitingCopy(job) {
  if (!job || job.started_at) return "";
  if (job.status !== "pending" && job.status !== "queued") return "";
  const m = Number(job.worker_wake_minutes);
  return m > 0
    ? `waiting for a worker to start (about ${m} minute${m === 1 ? "" : "s"})`
    : "waiting for a worker to start";
}

/** Begin a new run on this page. Clears everything scoped to the previous one and leaves
 *  identity alone. Used by the deep link, by a finished chat turn, and by a re-review.
 *  `sessionId` is the conversation the run came from - the one a "run again" must go to. A run
 *  opened by link passes none: whatever session the page holds later is not that run's. */
export function beginRun(jobId, { sessionId = null } = {}) {
  state.jobId = jobId || null;
  state.runSessionId = sessionId || null;
  if (jobId) state.runOrigins[jobId] = state.runSessionId;
  state.jobStatus = "";
  state.outcomeMessage = "";
  state.laneCursor = null;
}

