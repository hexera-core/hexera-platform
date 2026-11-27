// Responsibility: Carry the two answers to a delivered mesh - send a change to it, or accept it against a bar.
// Boundaries: it reports through one injected callback; what starting the new run means belongs to the entrypoint.

/* The two ways a user can answer a delivered mesh: send a change to it, or accept it against
 * a bar they state.
 *
 * Both were duplicated - `confirmDispute` lived in viewer.js and `acceptMesh` in review.js,
 * and each ended with the same six lines reaching into another module's state: reassigning
 * `jobId` and `_outcomeMsg`, clearing `UI.proc`, `UI.nodes`, `UI.tl` and `_liveCur`, then
 * calling `startPipeline`. Neither owned any of that.
 *
 * They now report through one injected `onRerun` callback. What "start the new run" means -
 * resetting the stage, moving the state, attaching the stream - belongs to the entrypoint that
 * composed those pieces, and is written once there instead of twice here.
 */
import { esc } from "../core/format.js";
import { disputeReview, requestChange } from "../api/endpoints.js";

let onRerun = () => {};
let say = () => {};
/** Installed by the entrypoint: onRerun (newJobId, message, disputedJobId, sessionId?) -> void, and say
 *  (text) -> void for Hexera's words in the chat. The disputed run is named so the new one can
 *  belong to the same conversation - not to whatever the page holds. */
export function configureDispute({ onRerun: fn, say: s } = {}) {
  onRerun = fn || (() => {});
  say = s || (() => {});
}

function modal(html) {
  const m = document.createElement("div");
  m.className = "v-modal";
  m.innerHTML = html;
  document.body.appendChild(m);
  // Escape closes it, and focus lands inside, so a keyboard user is not stranded behind it.
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const close = () => { document.removeEventListener("keydown", onKey); m.remove(); };
  document.addEventListener("keydown", onKey);
  m.querySelector("textarea, button")?.focus();
  return { el: m, close };
}

/** CASE A - SEND CHANGE: the marks and the words, sent from the result panel as they stand.
 *  The back end answers in words, which the chat shows as Hexera's; when it started a new run,
 *  this mesh stays on screen and usable with a slim progress line, until that run ends: its mesh
 *  then takes this one's place (the entrypoint's result handler), or, if it failed or was
 *  stopped, this mesh simply stays. When no run started (a question back, a mark it could not
 *  place), the marks and the words stay so the person can adjust and send again. */
export async function sendChange(job, flags, comment, { busy = () => {} } = {}) {
  busy(true);
  let d;
  try {
    d = await requestChange(job, { flags, comment });
  } catch (e) {
    busy(false);
    say("I could not send that change: " + e.message);
    return;
  }
  // a second look at the same mesh comes back without words of its own: one plain line stands in
  say(d.reply || (d.job_id ? LOOK_AGAIN : ""));
  if (!d.job_id) { busy(false); return; }
  revisions.set(d.job_id, { from: job, busy });
  progress(job, PROGRESS);
  onRerun(d.job_id, "", job, d.session_id);
}

const LOOK_AGAIN = "I'll look again at the spots you marked, then rebuild the mesh.";

const PROGRESS = "Rebuilding with your change...";
/* the new run's id -> the view it revises */
const revisions = new Map();

function progress(job, text) {
  const el = document.getElementById("v-prog-" + job);
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
}

/** The job a running revision revises, or null. */
export function revisionOf(job) { const r = revisions.get(job); return r ? r.from : null; }

/** The new run's current step, on the old view's progress line. */
export function revisionStep(job, step) {
  const r = revisions.get(job);
  if (r) progress(r.from, step ? `${PROGRESS} ${step}` : PROGRESS);
}

/** The new run ended. A delivered mesh has already taken the old view's place; otherwise the old
 *  view is free again, its marks and words where they were. */
export function revisionEnded(job) {
  const r = revisions.get(job);
  if (!r) return;
  revisions.delete(job);
  progress(r.from, "");
  r.busy(false);
}

/** CASE B - the reviewer held it back, and the user states a bar it should be judged against.
 *  The SAME mesh is re-reviewed; validity is never overridden and it can still fail. */
export function acceptMesh(job, findings) {
  const { el: m, close } = modal(`<div class="box">
    <h3>Is this mesh acceptable for your study?</h3>
    <div>The reviewer held it back on ${findings.length ? "<b>" + findings.map(esc).join("</b>, <b>") + "</b>" : "quality"}.
      Every validity gate passed. Say what is acceptable and why - this becomes part of the
      acceptance criteria, the mesh is re-reviewed against it, and if it clears that bar it is
      delivered. It can still fail.</div>
    <textarea id="v-acc-note" placeholder="e.g. 44% prism-layer coverage at the wing-body junction is fine - this is a pressure-distribution study, not a boundary-layer one."></textarea>
    <div class="row2"><button class="v-btn" id="v-acc-cancel">Cancel</button>
      <button class="v-btn primary" id="v-acc-go" disabled>Re-review against my bar</button></div></div>`);

  const ta = m.querySelector("#v-acc-note"), go = m.querySelector("#v-acc-go");
  ta.addEventListener("input", () => { go.disabled = !ta.value.trim(); });
  m.querySelector("#v-acc-cancel").onclick = close;
  go.onclick = async () => {
    go.disabled = true;
    try {
      const d = await disputeReview(job, { flags: [], comment: ta.value, mode: "accept" });
      close();
      document.getElementById("viewer-" + job)?.remove();
      onRerun(d.job_id,
        "Understood - I'll re-review the same mesh against the bar you just set, and deliver it if it clears.",
        job);
    } catch (e) {
      go.disabled = false;
      alert("Could not start the re-review: " + e.message);
    }
  };
}
