// Responsibility: Compose the modules and start the page - the only script index.html loads.
// Owns: every wire between modules, so none of them needs to know about another.
// Boundaries: nothing runs until the browser loads this file; startup lives here and nowhere else.

/* THE LIVE ENTRYPOINT. Composition and startup, and nothing else.
 *
 * Every wire between modules is made here, in one readable place: which renderer the event
 * dispatch drives, where the HTTP layer reports offline and 401, what "a run finished" means,
 * what "start a new run" means. The modules below it know none of that about each other.
 *
 * Startup used to be spread through the transport layer - importing stream.js mounted the
 * stage, checked API health and ran the deep-link handler. Nothing here runs until the browser
 * loads this module, and this module is the only thing index.html loads.
 */
import { apiFetch, setNotifier, useIdentity } from "./api/client.js";
import { getJob } from "./api/endpoints.js";
import { dispatch, resultSurface } from "./core/events.js";
import { beginRun, get as getState, set as setState } from "./core/state.js";
import { Notice } from "./render/notice.js";
import { Stage, closeLightbox, openLightbox, setResultHandler } from "./render/stage.js";
import { configure as configureStream, replay, start as startStream, terminalResult }
  from "./realtime/stream.js";
import { configureComposer, disableInput, enableInput, mountComposer, setPlaceholder }
  from "./shell/composer.js";
import { refreshHealth } from "./shell/settings.js";
import { configureDispute } from "./viewer/dispute.js";
import { openViewer } from "./viewer/viewer.js";

/* the HTTP layer's two shared failures, given a voice */
setNotifier({
  offline: () => Notice.hold("net",
    "You appear to be offline - reconnecting when your network returns.", "warn"),
  online: () => Notice.release("net"),
  unauthorized: () => Notice.hold("auth",
    "This deployment rejected the request as unauthorized.", "error"),
  authorized: () => Notice.release("auth"),
});

/* one event interpretation path, live and replayed alike */
function onEvent(ev) {
  setState.laneCursor(dispatch(ev, Stage, getState.laneCursor()));
}

configureStream({
  onEvent,
  onTerminal(job) {
    Stage.final(terminalResult(job, getState.outcomeMessage()));
    enableInput();
    setPlaceholder("Start a new simulation…");
  },
  onPollTrouble: () => Notice.hold("poll",
    "Having trouble reaching the server for status updates - still retrying.", "warn"),
  onPollRecovered: () => Notice.release("poll"),
});

/* what "a run finished" means: attach the viewer, or do not */
setResultHandler((data, anchor) => {
  const job = data.job || getState.jobId();
  if (!job) return;
  const surface = resultSurface(data);
  if (surface === "mesh") {
    // THE RESULT SURFACE IS THE MESH: open it inline, with the deliverables and the
    // flag -> re-review controls attached to the thing they describe.
    openViewer(job, anchor,
      { metrics: { attempts: data.attempts, pass: true }, files: data.files || [] });
  } else if (surface === "unreviewed") {
    openViewer(job, anchor,
      { metrics: { attempts: data.attempts, pass: false }, files: data.files || [],
        unreviewed: true, findings: data.findings || [], reasoning: data.reasoning || "" });
  }
  // else: a TERMINAL failure - no valid mesh was ever produced. Any mesh file left in the
  // workspace is structurally INVALID; showing it as "your mesh" would be a lie. The outcome
  // message already explains what happened - that is the whole result.
});

/* what "start a new run" means: one definition, used by three callers */
function attachJob(id, { replayHistory = false, message = "" } = {}) {
  if (message) Stage.chat("assistant", message);
  Stage.mount();
  beginRun(id);
  try { history.replaceState(null, "", location.pathname + "?job=" + id); } catch { /* ignore */ }
  Stage.ensureProc();
  startStream(replayHistory ? 0 : undefined);
}

configureComposer({
  notice: Notice,
  chat: (role, text) => Stage.chat(role, text),
  brief: (b) => Stage.brief(b),
  supportedCopy: (t) => Stage.setSupportedCopy(t),
  onJobStarted: (id) => attachJob(id),
});

configureDispute({
  onRerun: (id, message) => attachJob(id, { message }),
});

/* global keyboard affordances */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeLightbox();
});
// role="button" elements are not natively keyboard-operable - make Enter/Space activate them,
// so the header chips work without a mouse.
document.addEventListener("keydown", (e) => {
  const t = e.target;
  if ((e.key === "Enter" || e.key === " ") && t?.getAttribute?.("role") === "button") {
    e.preventDefault();
    t.click();
  }
});
document.getElementById("lb").addEventListener("click", closeLightbox);
// the browser's own signal - instant, no request needed
window.addEventListener("offline", () => Notice.hold("net",
  "You are offline - the run keeps going on the server; this page reconnects when your network returns.",
  "warn"));
window.addEventListener("online", () => { Notice.release("net"); refreshHealth(); });

/* boot */

function bootLive() {
  Stage.mount();
  mountComposer();
  refreshHealth();

  /* Deep link: /ui?job=<id>[&user=<owner>]
   *
   * ONE link, two jobs to do - a run can legitimately take hours and a user WILL reload the
   * page.
   *   still running -> re-attach to the live stream, replaying the history first
   *   finished      -> replay the whole run, then show the result and the mesh
   * The optional `user` parameter sets the identity for this page load; jobs are
   * owner-scoped. */
  const params = new URLSearchParams(location.search);
  const deepLinkJob = params.get("job");
  if (params.get("user")) useIdentity(params.get("user"));
  if (!deepLinkJob) return;

  (async () => {
    Stage.clearEmpty();
    let job = null;
    try {
      job = await getJob(deepLinkJob, { throwOnError: false });
    } catch { /* already reported by the HTTP boundary */ }
    if (!job) {
      Stage.chat("assistant", "I could not open that job. If it belongs to another account, "
        + "set the user in settings and reload.");
      return;
    }
    beginRun(deepLinkJob);
    setState.jobStatus(job.status);
    Stage.ensureProc();
    if (job.status === "succeeded" || job.status === "failed") {
      // REPLAY THE WHOLE RUN FIRST. A finished job still has its event log, and the point of
      // keeping one is that a user can see HOW they got their mesh, not just that they got it.
      await replay(deepLinkJob);
      Stage.final(terminalResult(job, getState.outcomeMessage()));
      return;
    }
    // LIVE. since=0 makes the server replay the entire log, then go live - a reload used to
    // cost the user the history of their run; now it costs them nothing.
    startStream(0);
    disableInput();
  })();
}

bootLive();

export { apiFetch };   // re-exported so a console session can exercise the HTTP boundary
