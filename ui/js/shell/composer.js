// Responsibility: Own the user's two inputs - the geometry upload and the chat composer.
// Boundaries: listeners are registered only when it is mounted, and it owns the state of its own controls.

/* The user's two inputs: the geometry upload and the chat composer.
 *
 * It wires DOM listeners, calls endpoints, and reports what happened. It owns the
 * enabled/disabled presentation of its own controls and nothing else - it used to also mutate
 * the URL, start the pipeline and hold `sessionId` as a global.
 *
 * Every listener is registered by `mountComposer`, called once by the entrypoint. Before, they
 * were registered at import time, so importing this file attached handlers.
 */
import { confirmGeometryCheck, getGeometryCheck, retryGeometryCheck, sendMessage, uploadGeometry }
  from "../api/endpoints.js";
import { acceptAttribute, isOfferable, loadIntakeFormats, refusalCopy, supportedCopy }
  from "./intake_formats.js";
import { get as getState, set as setState } from "../core/state.js";


const MAX_INPUT_HEIGHT = 140;

const $ = (id) => document.getElementById(id);

let deps = {
  notice: { show() {}, },
  chat() {},
  brief() {},
  onJobStarted() {},
  // advisory: the empty state's format line, filled from the server's capability description
  supportedCopy() {},
  // the geometry check's labelled picture and proposal, with the confirm action to call
  geometryCheck() {},
  // the card that stands in when the stage cannot draw, following the check in place
  geometryCard() {},
};
export function configureComposer(d) { deps = { ...deps, ...d }; }

export function enableInput() {
  const i = $("chat-input");
  i.disabled = false;
  i.placeholder = "Type your reply…";
  $("send-btn").disabled = false;
  i.focus();
}

export function disableInput() {
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
}

export function setPlaceholder(text) { $("chat-input").placeholder = text; }

/** THE FILE A RUN MESHES, named by the server. The label was only ever filled by this page's own
 *  upload, so a reload - or a run opened from a link - read "No geometry file selected" beside a
 *  run that plainly had one. A page that holds an upload of its own keeps naming that upload. */
export function showRunFile(name) {
  const lbl = $("file-label");
  if (!name || !lbl || getState.sessionId()) return;
  lbl.textContent = name; lbl.className = "ready";
}

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, MAX_INPUT_HEIGHT) + "px";
}

async function upload(file) {
  // One geometry per session, whatever the route in. The button is gone after a successful
  // upload, but a drag-and-drop reaches here directly and would otherwise replace the file
  // the intake conversation was built on.
  if (getState.sessionId()) {
    deps.notice.show("This session is already meshing " + $("file-label").textContent
      + ". Start a new session to use different geometry.", "warn");
    return;
  }
  // Advisory only - the SERVER validates every upload. When capability data cannot be fetched
  // the file is offered anyway and refused server-side if unsupported, so a degraded fetch
  // never becomes the security boundary.
  const intake = await loadIntakeFormats();
  if (!isOfferable(file.name, intake)) {
    // A native CAD file (a SolidWorks part, a Creo assembly...) is told which export to upload
    // instead, in the server's own words. It stays until dismissed or the next upload: the user
    // may be off exporting the STEP while it is there to read.
    deps.notice.show(refusalCopy(file.name, intake), "error", { sticky: true });
    return;
  }
  // The size limit is the server's to state: a file over it is refused before a byte is sent, in
  // words that say the limit and what to do instead.
  const btn = $("upload-btn"), lbl = $("file-label");
  btn.disabled = true; btn.textContent = "Uploading…"; lbl.textContent = file.name;
  // A large file travels straight to storage and can take minutes, so the button counts it up,
  // then says what happens while the server reads it back.
  const progress = (p) => {
    if (p.phase === "sending" && p.total) {
      btn.textContent = "Uploading… " + Math.min(99, Math.floor((100 * p.sent) / p.total)) + "%";
    } else if (p.phase === "checking") {
      btn.textContent = "Checking the file…";
    }
  };
  try {
    const d = await uploadGeometry(file, { onProgress: progress });
    setState.sessionId(d.session_id);
    lbl.textContent = d.step_filename; lbl.className = "ready";
    // The geometry a session was opened on is FINAL: the intake conversation, the admission
    // verdict and the approved contract are all bound to it, so swapping the file underneath
    // them would silently invalidate every answer given so far. The control is removed, not
    // disabled, so there is nothing to re-enable by accident.
    btn.remove();
    $("step-file-input").disabled = true;
    enableInput();
    if (d.intake_greeting) deps.chat("assistant", d.intake_greeting);
    watchGeometryCheck(d.session_id);
  } catch (e) {
    btn.disabled = false; btn.textContent = "Upload geometry";
    lbl.textContent = "No geometry file selected"; lbl.className = "";
    // stays until dismissed or the next upload: a refusal says what to do instead, and that
    // takes longer to read, and to act on, than a notice that fades
    if (e.status !== 401) deps.notice.show("Upload failed: " + e.message, "error", { sticky: true });
  }
}

/* THE GEOMETRY CHECK. The worker scouts the upload as soon as it is stored and names the
   stickers once the user has answered the opening question; this polls until the named check is
   ready and hands it to the stage to show. A check that is off, or reads a file it cannot scout,
   simply never appears - the intake asks as it always did. A check that gave up is said so, with
   the way on: try again, or carry on in the chat. The wait is long on purpose: the naming waits
   for the user, and a user may take their time over the first answer. */
const CHECK_POLL_MS = 3000, CHECK_POLL_MAX = 900;   // forty-five minutes, then stop quietly

/* THE CHAT WAITS WITH THE USER. From the moment the reply says the part is being drawn until the
   user has pressed Proceed (or the check gave up), the box is closed: a message typed meanwhile
   would run the intake past the stage. The wait is bounded so a stage that never comes cannot
   lock the conversation for good. */
const DRAWING_PREFIX = "GEOMETRY CHECK (drawing your part):";
const DRAWING_WAIT_MS = 3 * 60 * 1000, STAGE_WAIT_MS = 10 * 60 * 1000;
let _holdTimer = null, _held = false;
// what the poll last saw of the check, so a chat reply that arrives late installs the hold that
// matches: none once the check gave up or was confirmed, the stage's once the stage is open
let _checkState = null;
function holdInput(placeholder, ms) {
  _held = true; disableInput(); setPlaceholder(placeholder);
  clearTimeout(_holdTimer); _holdTimer = setTimeout(releaseHold, ms);
}
export function holdForDrawing() {
  if (_checkState === "done" || _checkState === "over") return enableInput();
  if (_checkState === "ready") return holdInput("Check the openings beside this chat and press Proceed…", STAGE_WAIT_MS);
  holdInput("Drawing your part… it will appear beside this chat in a moment", DRAWING_WAIT_MS);
}
export function releaseHold() {
  if (!_held) return;
  _held = false; clearTimeout(_holdTimer); _holdTimer = null;
  setPlaceholder("Type your reply…"); enableInput();
}
export function isHeld() { return _held; }
/* support hook for the ui tier: what the poll has seen and whether the box is held */
export function holdState() { return { held: _held, check: _checkState }; }
export function noteCheckState(s) { _checkState = s; }

async function watchGeometryCheck(sessionId) {
  // THE STAGE OPENS AS SOON AS THE PART IS MEASURED, with the measuring step's own labels under
  // a banner that says what is happening, and takes the model's labels when they arrive. The
  // user is already turning the part while the model thinks - and can proceed without it.
  let stage = null, stageFailed = false, told = "";
  // THE WAY ON when a step gave up: the scout when the part was never measured, the naming when
  // it was. The watch goes on, so the fresh result lands where the old one would have.
  const retryFn = async (step) => { const d = await retryGeometryCheck(sessionId, step); told = ""; return d; };
  // `edits` is the stage's (or the card's) one line saying what the user changed on the form. It
  // is said once the server holds the answers, before the declaration that carries them all.
  const confirmFn = async (body, edits) => {
    const reply = await confirmGeometryCheck(sessionId, body);
    if (edits) deps.chat("assistant", edits);
    deps.chat("assistant", reply.message);
    // THE INTAKE PICKS UP: the confirm ran one chat turn in the user's name, and its reply
    // is the next question - or the dispatch, when nothing was left to ask.
    _checkState = "done";
    releaseHold();
    if (reply.next) {
      if (reply.continued_with) deps.chat("user", reply.continued_with);
      if (reply.next.reply) deps.chat("assistant", reply.next.reply);
      deps.brief(reply.next.brief);
      if (reply.next.done && reply.next.job_id) { disableInput(); deps.onJobStarted(reply.next.job_id); }
    }
    return reply;
  };
  for (let n = 0; n < CHECK_POLL_MAX; n++) {
    if (getState.sessionId() !== sessionId) return;
    let d = null;
    try { d = await getGeometryCheck(sessionId); } catch { /* transient; try again */ }
    const status = d && d.status;
    if (d && d.confirmed) {
      // answered, on the stage or the card, whatever the naming did afterwards
      _checkState = "done"; releaseHold(); if (stage) stage.release(); return;
    }
    if (status === "off" || status === "unsupported" || (status === "failed" && !stage && !d.retry)) {
      if (status === "failed" && d.reason) {
        deps.notice.show("The geometry check could not read this file; the questions will "
          + "cover it instead.", "warn");
      }
      _checkState = "over";
      releaseHold();                               // nothing to wait for any more
      return;
    }
    if (status === "failed" && !stage) {
      // THE CHECK GAVE UP BEFORE THE PART WAS DRAWN - a drawing that took too long, a worker
      // that died - and can be run again. Said once per outcome, with the way on: try again, or
      // carry on in the chat, which asks what the picture would have settled. The watch goes on:
      // a retry, or a slow worker landing after all, brings the check back.
      const key = "failed|" + (d.reason || "");
      if (told !== key) {
        told = key;
        deps.notice.show("The geometry check did not finish: " + (d.reason || "it gave up")
          + ". Try again, or just carry on in the chat.", "warn",
          { sticky: true, action: "Try again", onAction: () => {
            deps.notice.clear();
            retryFn(d.retry).catch((e) => deps.notice.show("Could not run the geometry check again: " + e.message, "error"));
          } });
      }
      _checkState = "over";
      releaseHold();
      if (stageFailed) deps.geometryCard(d);     // the card keeps the part; Proceed follows the unit
    } else if (status === "failed" && stage) {
      // the naming gave up after the stage opened: the code's labels stand, the user proceeds -
      // or runs the naming again from the stage, so the watch goes on
      stage.update(d); _checkState = "ready";
      if (isHeld()) holdInput("Check the openings beside this chat and press Proceed…", STAGE_WAIT_MS);
      if (!d.retry) return;
    } else if (status === "scouted") {
      if (d.skin && !stage && !stageFailed) {
        stage = await deps.geometryCheck(sessionId, d, confirmFn, retryFn);   // null when the stage cannot draw
        if (stage === null) stageFailed = true;                               // the card stands in
      }
      if (stage) stage.update(d);            // the banner: waiting on the chat, the unit, or the naming
      else if (stageFailed) deps.geometryCard(d);   // the card's Proceed follows the unit, in place
    } else if (status === "ready" && d.named !== false) {
      _checkState = "ready";
      if (isHeld()) holdInput("Check the openings beside this chat and press Proceed…", STAGE_WAIT_MS);
      if (stage) stage.update(d); else await deps.geometryCheck(sessionId, d, confirmFn, retryFn);
      return;
    }
    await new Promise((r) => setTimeout(r, CHECK_POLL_MS));
  }
}

/* AFTER A RUN ENDS THE CONVERSATION GOES ON. The next message starts another run on the same
   geometry, with the last requirements as the proposal - the server takes it from there. The two
   common answers get a chip each so they are one click; anything typed works the same way. The
   offer is made only while this page holds the session: a deep-linked run has no conversation
   here to continue. */
const RUN_AGAIN_TEXT = "Run it again with the same requirements.";
let _offerEl = null;

export function offerNewRun() {
  clearNewRunOffer();
  const inner = $("input-inner");
  if (!getState.sessionId() || !inner) return;
  const row = document.createElement("div");
  row.className = "rerun"; row.id = "rerun";
  const lead = document.createElement("span");
  lead.className = "rerun-lead"; lead.textContent = "Next:";
  const again = document.createElement("button");
  again.type = "button"; again.className = "rerun-chip"; again.id = "rerun-again";
  again.textContent = "Run again";
  again.onclick = () => sendText(RUN_AGAIN_TEXT);
  const change = document.createElement("button");
  change.type = "button"; change.className = "rerun-chip"; change.id = "rerun-change";
  change.textContent = "Change something";
  change.onclick = () => {
    clearNewRunOffer();
    enableInput();
    setPlaceholder("Tell me what to change, and I will set up the next run…");
  };
  row.append(lead, again, change);
  inner.parentElement.insertBefore(row, inner);
  _offerEl = row;
}

export function clearNewRunOffer() {
  if (_offerEl) { _offerEl.remove(); _offerEl = null; }
}

async function send() {
  const inp = $("chat-input");
  await sendText(inp.value.trim());
}

async function sendText(txt) {
  const inp = $("chat-input");
  const sessionId = getState.sessionId();
  if (!txt || !sessionId) return;
  clearNewRunOffer();
  inp.value = ""; autoResize(inp);
  deps.chat("user", txt);
  disableInput();
  try {
    const d = await sendMessage(sessionId, txt);
    deps.chat("assistant", d.reply);
    deps.brief(d.brief);
    if (d.done && d.job_id) deps.onJobStarted(d.job_id);
    else if (String(d.reply || "").startsWith(DRAWING_PREFIX)) holdForDrawing();
    else enableInput();
  } catch (e) {
    if (e.status !== 401) {
      deps.chat("assistant", "Something went wrong sending that: " + e.message + ". Please try again.");
    }
    enableInput();
  }
}

export function mountComposer() {
  // Fill the picker's advisory accept list from the backend, so what the browser OFFERS and what
  // the server ACCEPTS cannot drift. Left empty when capabilities are unavailable: the picker
  // then offers every file and the server refuses anything unsupported.
  loadIntakeFormats().then((intake) => {
    const input = $("step-file-input");
    if (input) input.setAttribute("accept", acceptAttribute(intake));
    // The same response drives the empty state's format line, so the picker's accept list and
    // the sentence a user reads can never name different formats.
    if (intake) deps.supportedCopy(supportedCopy(intake));
  });
  $("step-file-input").addEventListener("change", function () {
    const f = this.files[0];
    if (f) upload(f);
  });
  $("upload-btn").addEventListener("click", () => $("step-file-input").click());

  document.body.addEventListener("dragover", (e) => {
    e.preventDefault(); document.body.classList.add("dragover");
  });
  document.body.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) document.body.classList.remove("dragover");
  });
  document.body.addEventListener("drop", (e) => {
    e.preventDefault(); document.body.classList.remove("dragover");
    const f = e.dataTransfer.files[0];
    if (f) upload(f);
  });

  $("send-btn").addEventListener("click", send);
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("chat-input").addEventListener("input", function () { autoResize(this); });
}
