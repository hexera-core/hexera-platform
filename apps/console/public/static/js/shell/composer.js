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
import { uploadGeometry, sendMessage } from "../api/endpoints.js";
import { acceptAttribute, isOfferable, loadIntakeFormats, supportedCopy }
  from "./intake_formats.js";
import { get as getState, set as setState } from "../core/state.js";


const LARGE_FILE_BYTES = 500 * 1024 * 1024;
const MAX_INPUT_HEIGHT = 140;

const $ = (id) => document.getElementById(id);

let deps = {
  notice: { show() {}, },
  chat() {},
  brief() {},
  onJobStarted() {},
  // advisory: the empty state's format line, filled from the server's capability description
  supportedCopy() {},
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
    deps.notice.show(supportedCopy(intake), "error");
    return;
  }
  if (file.size > LARGE_FILE_BYTES) {
    deps.notice.show("That file is very large (" + Math.round(file.size / 1048576)
      + " MB). Uploads over ~500 MB may be rejected - try a decimated surface.", "warn");
  }
  const btn = $("upload-btn"), lbl = $("file-label");
  btn.disabled = true; btn.textContent = "Uploading…"; lbl.textContent = file.name;
  try {
    const d = await uploadGeometry(file);
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
  } catch (e) {
    btn.disabled = false; btn.textContent = "Upload geometry";
    lbl.textContent = "No geometry file selected"; lbl.className = "";
    if (e.status !== 401) deps.notice.show("Upload failed: " + e.message, "error");
  }
}

async function send() {
  const inp = $("chat-input"), txt = inp.value.trim();
  const sessionId = getState.sessionId();
  if (!txt || !sessionId) return;
  inp.value = ""; autoResize(inp);
  deps.chat("user", txt);
  disableInput();
  try {
    const d = await sendMessage(sessionId, txt);
    deps.chat("assistant", d.reply);
    deps.brief(d.brief);
    if (d.done && d.job_id) deps.onJobStarted(d.job_id);
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
