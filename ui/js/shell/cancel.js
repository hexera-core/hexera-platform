// Responsibility: Carry the owner's decision to stop a run - the confirm step, the reason, the call.
// Boundaries: it reports nothing back. The stream and the poller deliver a cancelled run's ending
//             exactly as they deliver every other, so cancelling renders through the one terminal path.

/* Stopping a run is the one thing on this page that cannot be undone from the page, so it is the
 * one thing the page asks about first. The modal is the same shape as the re-review dialogs in
 * viewer/dispute.js: a question, a reason box, a way out, and one primary action.
 */
import { cancelJob } from "../api/endpoints.js";

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

/** Ask, then stop. `onError` receives a plain sentence when the API refuses (a run that already
 *  finished, a network failure); the modal stays open so the user can try again or keep the run. */
export function confirmCancel(jobId, { onError } = {}) {
  if (!jobId) return;
  const { el: m, close } = modal(`<div class="box"><h3>Stop this run?</h3>
    <div>The run ends now and cannot be resumed. A cancelled run is not charged, and you can
      start a new one straight away.</div>
    <textarea id="c-reason" maxlength="500" placeholder="Why are you stopping it? (optional - kept with the run)"></textarea>
    <div class="row2"><button class="v-btn" id="c-keep">Keep running</button>
    <button class="v-btn primary" id="c-go">Cancel run</button></div></div>`);

  m.querySelector("#c-keep").onclick = close;
  const go = m.querySelector("#c-go");
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Cancelling…";
    try {
      await cancelJob(jobId, m.querySelector("#c-reason").value || "");
      // Nothing to render here: the closing line arrives on the stream and the poller sees the
      // status, the same way a finished run reaches the page.
      close();
    } catch (e) {
      go.disabled = false;
      go.textContent = "Cancel run";
      (onError || ((msg) => alert(msg)))("Could not cancel the run: " + e.message);
    }
  };
}
