// Responsibility: Carry the two answers to a delivered mesh - flag it for rebuild, or accept it against a bar.
// Boundaries: it reports through one injected callback; what starting the new run means belongs to the entrypoint.

/* The two ways a user can answer a delivered mesh: flag it for rebuild, or accept it against
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
import { disputeReview } from "../api/endpoints.js";

let onRerun = () => {};
/** Installed by the entrypoint: (newJobId, message) -> void. */
export function configureDispute({ onRerun: fn } = {}) { onRerun = fn || (() => {}); }

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

/** CASE A - the reviewer passed it but the user disagrees: re-review AND rebuild. */
export function flagDispute(job, flags) {
  const hasFlags = flags.length > 0;
  const { el: m, close } = modal(`<div class="box"><h3>Re-review & rebuild this mesh?</h3>
    <div>${hasFlags
      ? `The reviewer will re-inspect the delivered mesh at your ${flags.length} flagged spot${flags.length > 1 ? "s" : ""},`
      : "No spots flagged - describe the change you want below (flagging exact spots is recommended when the issue is localized). The reviewer will evaluate your request against the delivered mesh,"}
    then the pipeline will <b>rebuild the mesh</b> and re-review it against your concerns plus all original criteria.</div>
    <div style="margin-top:8px;color:#6a6e77">This is a full mesh run - typically 1-2 hours of compute.</div>
    <textarea id="v-comment" placeholder="${hasFlags ? "Anything else the reviewer should know? (optional)" : "What should change? e.g. “I want finer resolution in the wake region” (required)"}"></textarea>
    <div class="row2"><button class="v-btn" id="v-cancel">Cancel</button>
    <button class="v-btn primary" id="v-go"${hasFlags ? "" : " disabled"}>Start re-review & rebuild</button></div></div>`);

  const go = m.querySelector("#v-go");
  if (!hasFlags) {
    const ta = m.querySelector("#v-comment");
    ta.addEventListener("input", () => { go.disabled = !ta.value.trim(); });
  }
  m.querySelector("#v-cancel").onclick = close;
  go.onclick = async () => {
    const comment = m.querySelector("#v-comment").value || "";
    go.disabled = true;
    try {
      const d = await disputeReview(job, { flags, comment });
      close();
      document.getElementById("viewer-" + job)?.remove();
      onRerun(d.job_id,
        `Re-review started for your flagged region${flags.length > 1 ? "s" : ""}. New job: \`${d.job_id}\``);
    } catch (e) {
      go.disabled = false;
      alert("Could not start the re-review: " + e.message);
    }
  };
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
        "Understood - I'll re-review the same mesh against the bar you just set, and deliver it if it clears.");
    } catch (e) {
      go.disabled = false;
      alert("Could not start the re-review: " + e.message);
    }
  };
}
