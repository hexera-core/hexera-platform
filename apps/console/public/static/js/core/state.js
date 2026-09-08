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
  jobStatus: "",        // last status the poller saw: '' | running | succeeded | failed
  outcomeMessage: "",   // the closing text, held until the result card is built
  laneCursor: null,     // which timeline lane the event stream is currently in
  userId: "",           // identity for this page load (settings or ?user=)
};

export const get = {
  sessionId: () => state.sessionId,
  jobId: () => state.jobId,
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

/** True once the run reached a terminal state - the single place that decides it, so the
 *  poller, the reconnect scheduler and the composer cannot disagree. */
export function isTerminal() {
  return state.jobStatus === "succeeded" || state.jobStatus === "failed";
}

/** Begin a new run on this page. Clears everything scoped to the previous one and leaves
 *  identity alone. Used by the deep link, by a finished chat turn, and by a re-review. */
export function beginRun(jobId) {
  state.jobId = jobId || null;
  state.jobStatus = "";
  state.outcomeMessage = "";
  state.laneCursor = null;
}

