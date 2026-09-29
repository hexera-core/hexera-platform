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
import { beginRun, get as getState, isTerminalStatus, set as setState, waitingCopy }
  from "./core/state.js";
import { Notice } from "./render/notice.js";
import { Stage, closeLightbox, openLightbox, setCancelHandler, setResultHandler }
  from "./render/stage.js";
import { configure as configureStream, replay, start as startStream, terminalResult }
  from "./realtime/stream.js";
import { confirmCancel } from "./shell/cancel.js";
import { clearNewRunOffer, configureComposer, disableInput, enableInput, mountComposer,
  offerNewRun, setPlaceholder } from "./shell/composer.js";
import { refreshHealth } from "./shell/settings.js";
import { configureDispute } from "./viewer/dispute.js";
import { openGeometryStage } from "./viewer/geometry_stage.js";
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
    // ONLY THE CONVERSATION A RUN CAME FROM CAN RUN IT AGAIN. A run opened from a link has no
    // conversation on this page: with no session at all a message would go nowhere, so the box
    // stays closed and says what does work here - a new upload, which opens a new session. A
    // session a later upload opened is for OTHER geometry: its composer is its own conversation's
    // and is left exactly as it is, with no offer that would send "run again" to the wrong part.
    const session = getState.sessionId();
    if (!session || session !== getState.runSessionId()) {
      if (!session) {
        disableInput();
        setPlaceholder("Upload a geometry file to start a new session…");
      }
      return;
    }
    // THE RUN IS OVER, THE CONVERSATION IS NOT: the same session takes the next run on this
    // geometry - the same requirements again, or a change - and the composer says so.
    enableInput();
    setPlaceholder("Run it again, or tell me what to change…");
    offerNewRun();
  },
  // WHILE THE JOB WAITS FOR A WORKER the header says so, with the backend's estimate; once a
  // worker has it the line goes back to "working…" and the timeline takes over.
  onStatus: (job) => Stage.waiting(waitingCopy(job)),
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

/* what "start a new run" means: one definition, used by three callers. `origin` is the
   conversation the run belongs to: the page's own session for a run the chat started, the
   disputed run's conversation for a re-review. */
function attachJob(id, { replayHistory = false, message = "", origin = getState.sessionId() } = {}) {
  if (message) Stage.chat("assistant", message);
  clearNewRunOffer();      // a run is starting; the "run again" offer belonged to the last one
  Stage.mount();
  beginRun(id, { sessionId: origin });
  // WHERE THE RUN LIVES IN THE URL. The console routes runs at /runs/<id>; ui/index.html has no
  // routes and keeps the query string it has always used. Neither global set means the second
  // branch, which is today's behaviour byte for byte.
  try {
    const routed = globalThis.__HEXERA_ROUTED__;
    history.replaceState(null, "",
      routed ? "/runs/" + id : location.pathname + "?job=" + id);
  } catch { /* ignore */ }
  Stage.ensureProc();
  Stage.showCancel();
  startStream(replayHistory ? 0 : undefined);
}

configureComposer({
  notice: Notice,
  chat: (role, text) => Stage.chat(role, text),
  brief: (b) => Stage.brief(b),
  supportedCopy: (t) => Stage.setSupportedCopy(t),
  onJobStarted: (id) => attachJob(id),
  // THE GEOMETRY CHECK TAKES THE STAGE, the way a delivered mesh does: the part in 3D with its
  // stickers, the form beside it. When the stage cannot open, the same form arrives as a card.
  geometryCheck: (sessionId, d, confirm, retry) => openGeometryStage(sessionId, d, confirm,
    { anchorEl: Stage.col(), retry, fallback: () => { Stage.geometryCheck(d, confirm); } }),
  // the card, when it stands in for the stage, follows the check in place
  geometryCard: (d) => Stage.geometryCheckUpdate(d),
});

configureDispute({
  // A RE-REVIEW BELONGS TO THE DISPUTED RUN'S CONVERSATION, not to whatever session the page
  // holds now: a run opened by link, disputed after an upload of other geometry, has none, and
  // its "run again" must never reach that other geometry's session.
  onRerun: (id, message, disputedJobId) => attachJob(id,
    { message, origin: getState.runOrigin(disputedJobId) }),
});

/* what "stop this run" means: ask first, then the API. The ending arrives through the stream and
   the poller like every other, so a cancelled run renders through the one terminal path. */
setCancelHandler(() => confirmCancel(getState.jobId(),
  { onError: (msg) => Stage.chat("assistant", msg) }));

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
  // TWO WAYS TO NAME THE RUN, one meaning. The query string is the original contract and still
  // the only one ui/index.html uses; the global is how the console's /runs/<id> route hands the
  // id over without a redirect through a query string it would then have to clean up.
  const deepLinkJob = params.get("job") || globalThis.__HEXERA_BOOT_JOB__ || null;
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
    Stage.waiting(waitingCopy(job));
    if (isTerminalStatus(job.status)) {
      // REPLAY THE WHOLE RUN FIRST. A finished job still has its event log, and the point of
      // keeping one is that a user can see HOW they got their mesh, not just that they got it.
      await replay(deepLinkJob);
      Stage.final(terminalResult(job, getState.outcomeMessage()));
      return;
    }
    // LIVE. since=0 makes the server replay the entire log, then go live - a reload used to
    // cost the user the history of their run; now it costs them nothing.
    startStream(0);
    Stage.showCancel();
    disableInput();
  })();
}

bootLive();

export { apiFetch };   // re-exported so a console session can exercise the HTTP boundary
