/* Responsibility: Tell the customer their CAD was repaired, and hand them the file - on any
   ending, including a run whose mesh never arrived. */
/* Boundaries: it renders what the API already delivered. It fetches nothing, decides no status,
   and makes no claim about the mesh - that is the outcome message's job. */

import { esc, fmtBytes } from "../core/format.js";

/* WHY THIS IS NOT PART OF THE VIEWER. The viewer is the mesh's surface, and it only opens on
   proof that a mesh passed a gate (core/events.js resultSurface). A repair is a DIFFERENT
   deliverable with its own fate: the geometry can be fixed and the mesh still fail. Hanging the
   repaired file off the viewer meant a repair we had already done, and already stored, was
   withheld from the person who paid for it because a later stage went wrong. */

/* The repair's own deliverables, in the order a customer uses them: the fixed geometry first,
   the explanation of what changed second. Any other class belongs to another panel. */
const _ORDER = ["repaired_cad", "repair_report"];

export function repairDeliverables(files) {
  const have = (files || []).filter((f) => f && _ORDER.includes(f.type));
  return _ORDER.flatMap((type) => have.filter((f) => f.type === type));
}

/** Draw the repair panel at `anchorEl`, or draw nothing and return null if nothing was repaired.
 *
 *  Returns the element so a caller can scroll to it. Idempotent in the job id: a re-render or a
 *  reconnect must not stack two panels on one run.
 */
export function openRepairPanel(job, anchorEl, data) {
  const files = repairDeliverables((data || {}).files);
  if (!files.length || !anchorEl) return null;
  const existing = document.getElementById("repair-" + job);
  if (existing) return existing;

  const box = document.createElement("div");
  box.className = "viewer";
  box.id = "repair-" + job;
  const downloads = files.map((f, i) => {
    // The repaired geometry leads in the accent, the way the mesh case does in the viewer: it is
    // the thing the customer came for.
    const primary = i === 0 ? " primary" : "";
    return `<a class="v-dlbtn${primary}" href="${esc(f.url || "#")}" download
               target="_blank" rel="noopener"><b>${esc(f.label || f.type)}</b
               ><span>${fmtBytes(f.size)}</span></a>`;
  }).join("");
  // WHAT THE HEADLINE MAY CLAIM. Only that the geometry was repaired, which is what the presence
  // of a `repaired_cad` row proves. It says nothing about the mesh: on this path there isn't one,
  // and the outcome message above has already explained why.
  const repaired = files.some((f) => f.type === "repaired_cad");
  const headline = repaired
    ? "Your CAD was repaired"
    : "What the inspection found in your CAD";
  const line = repaired
    ? "We fixed the geometry in your upload and this is the result. Your original file is "
      + "unchanged - the repair is a new file, and the report says what was done to it."
    : "The inspection could not repair this automatically. The report says what it found.";
  // The panel borrows the result panel's own sections rather than inventing a look: a head with
  // the title, the explanation in the `v-why` band that wraps, and the downloads as a `v-dl`
  // column. `v-status` is deliberately NOT used for the prose - it is a single ellipsised line.
  box.innerHTML = `<div class="v-bar">
      <div class="v-sec v-head">
        <div class="v-row v-titlerow"><b class="v-title">${esc(headline)}</b></div>
      </div></div>
    <div class="v-why">${esc(line)}</div>
    <div class="v-sec"><div class="v-dl">${downloads}</div></div>`;
  anchorEl.appendChild(box);
  return box;
}
