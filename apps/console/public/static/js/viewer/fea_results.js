// Responsibility: The few FEA lines the short result screen keeps in sight (revamp 5/5): what the mesh is, an open bonded pair in red with the way on (PLAN-fea-21), how the parts are joined in one line, and every CAD face accounted for (MESH-8) in one line.
// Boundaries: markup only. Every limit, every place, the plan in words, the full hot-spot list, the per-solver checks, the per-pair table and the named faces are in the Mesh report (render/mesh_report.py); the hot spots by kind are in viewer/result_panel.js.
// Collaborates with: viewer/viewer.js (mounts it when the payload carries `fea`), engines/fea_results.py (builds the block it shows).
import { esc } from "../core/format.js";

/** An assembly in short: an open bonded pair in red (never a quiet pass), then how the parts are joined. */
function assemblyHtml(a) {
  if (!a) return "";
  const opened = a.open_bonded || [];
  const open = opened.length ? `<div class="fe-open" role="alert">
        <b>${opened.length === 1 ? "A bonded pair is open" : `${opened.length} bonded pairs are open`}</b>: the solver will not join these faces.
        <ul>${opened.map((o) => `<li>${esc(o.words)}</li>`).join("")}</ul></div>` : "";
  return `<div class="fe-asm">${open}<div class="tr-sum">${esc(a.words || "")}</div></div>`;
}

/** The FEA lines of the result screen (`f` = the payload's `fea` block). */
export function feaResultsHtml(job, f) {
  if (!f) return "";
  return `<div class="v-sec tr fe" id="v-fea-sec-${esc(job)}">
      ${f.view_note ? `<div class="tr-sum fe-view-note" role="note">${esc(f.view_note)}</div>` : ""}
      ${assemblyHtml(f.assembly)}
      ${f.faces_short ? `<div class="tr-sum fe-faces-short">${esc(f.faces_short)}</div>` : ""}
    </div>`;
}

/** The section has no controls of its own any more; the viewer's hooks still hold one handle. */
export function bindFeaResults(root, job, f) {
  if (!root || !f) return null;
  return { pick: () => {} };
}
