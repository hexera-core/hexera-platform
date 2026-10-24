// Responsibility: Show a delivered FEA mesh the way a structural engineer reads it (the V1 result screen, design 3.8): the quality in plain words against each solver limit, the through-wall check, the units, the plan in words, the hot spots, every named face (click one to see it alone), and what the download holds.
// Boundaries: markup and the panel's own controls. It draws nothing on the 3D canvas itself - the viewer hands it `hooks` (show a face alone, point at a hot spot) and owns the actors.
// Collaborates with: viewer/viewer.js (mounts it when the payload carries `fea`), engines/fea_results.py (builds the block it shows).
import { esc } from "../core/format.js";

const fmt = (v) => (v === null || v === undefined || v === "") ? "—"
  : (typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString() : Number(v.toPrecision(3)).toLocaleString()) : String(v));
const OUTCOME = { pass: "PASS", concerns: "PASS WITH NOTES", unmeasured: "NOT FULLY CHECKED", fail: "FAIL" };
const OP = { "<=": "at most", ">=": "at least", "==": "exactly", "<": "under", ">": "over" };

function checkRow(r) {
  const cls = r.ok === false ? " short" : r.ok === null || r.ok === undefined ? " unm" : "";
  const mark = r.ok === false ? "✕" : r.ok === null || r.ok === undefined ? "–" : "✓";
  return `<tr class="fe-ck${cls}"><td class="fe-mk">${mark}</td><td class="nm"><span>${esc(r.label)}</span></td>`
    + `<td class="n">${esc(fmt(r.measured))}</td><td class="n"><small>${esc(OP[r.op] || r.op)} ${esc(fmt(r.limit))}</small></td></tr>`;
}

function faceRow(f) {
  return `<tr class="tr-row fe-face" data-face="${esc(f.name)}" tabindex="0">`
    + `<td class="nm"><span>${esc(f.name)}</span>${f.cad_name ? `<small>${esc(f.cad_name)}</small>` : ""}</td>`
    + `<td>${esc(f.kind || "")}</td><td class="n">${f.area_mm2 != null ? esc(fmt(f.area_mm2)) + "<small> mm²</small>" : "—"}</td></tr>`;
}

const SOLVER_OUT = { pass: "PASS", warnings: "WARNINGS", fail: "FAIL", unmeasured: "NOT FULLY CHECKED" };

function solverRow(sv) {
  const cls = sv.outcome === "fail" ? " fail" : sv.outcome === "pass" ? "" : " concerns";
  const rows = (sv.rows || []).map((r) => `<li class="fe-sv-${esc(r.status)}">${esc(r.label)}: ${esc(fmt(r.measured))}`
    + `${r.warn != null ? ` <small>(warns ${esc(OP[r.op] ? (r.op === "<=" ? "over" : "under") : r.op)} ${esc(fmt(r.warn))})</small>` : ""}`
    + `${r.error != null ? ` <small>(fails ${esc(r.op === "<=" ? "over" : "under")} ${esc(fmt(r.error))})</small>` : ""}`
    + `<small class="fe-src">${esc(r.source)}</small></li>`).join("");
  return `<div class="fe-sv" data-solver="${esc(sv.solver)}">
      <div class="fe-sv-h"><b>${esc(sv.label)}</b> <span class="v-pill${cls}">${esc(SOLVER_OUT[sv.outcome] || "")}</span>
        <span class="fe-sv-file">${esc(sv.file)} · ${esc(sv.element)}${sv.file_ready === false ? " · isn't available" : ""}</span></div>
      <div class="fe-sv-t${sv.tested ? " ok" : ""}">${esc(sv.tested_words || "")}</div>
      <details><summary>${esc(sv.words || "")}</summary><ul class="fe-sv-rows">${rows}</ul></details>
    </div>`;
}

function spotRow(h) {
  const short = h.short ? ` <small class="fe-short">short of its rule</small>` : "";
  return `<li class="fe-spot" data-spot="${esc(h.id)}" data-face="${esc((h.faces || [])[0] || "")}" tabindex="0">`
    + `<b>${esc(h.id)}</b> ${esc(h.words)}${short}<small>${esc((h.faces || []).join(", "))}</small></li>`;
}

/** The panel section for one delivered FEA mesh (`f` = the payload's `fea` block). */
export function feaResultsHtml(job, f) {
  if (!f) return "";
  const ck = f.check || {}, tw = f.through_wall || {}, faces = f.faces || [], spots = f.hot_spots || [];
  const dl = f.download || {};
  return `<div class="v-sec tr fe" id="v-fea-sec-${esc(job)}">
      <div class="v-sec-h">${esc(f.title || "FEA mesh")} <span class="v-pill${ck.outcome === "fail" ? " fail" : (ck.outcome === "concerns" || ck.outcome === "unmeasured") ? " concerns" : ""}">${esc(OUTCOME[ck.outcome] || "")}</span></div>
      <div class="tr-sum">${esc(f.summary || "")}</div>
      ${f.view_note ? `<div class="tr-sum fe-view-note" role="note">${esc(f.view_note)}</div>` : ""}
      <ul class="tr-q">
        <li>${esc(ck.words || "")}</li>
        <li><b>Through-wall check:</b> ${esc(tw.words || "")}</li>
        <li><b>Units:</b> ${esc((f.units || {}).words || "")}</li>
      </ul>
    </div>
    <div class="v-sec fe-check">
      <div class="v-sec-h">Quality, against each limit</div>
      <div class="tr-wrap"><table class="tr-tab"><tbody>${(ck.rows || []).map(checkRow).join("")}</tbody></table></div>
      <div class="tr-hint">${esc(ck.solvers || "")}</div>
    </div>
    ${(f.solvers || []).length ? `<div class="v-sec fe-solvers">
      <div class="v-sec-h">Per solver</div>
      ${(f.solvers || []).map(solverRow).join("")}
      <div class="tr-hint">Each solver's own published element checks. Pick your solver's file from the downloads above;
        every file has the same node and element numbers and the same named faces.</div>
    </div>` : ""}
    <div class="v-sec fe-plan">
      <div class="v-sec-h">The plan, in words</div>
      <ul class="tr-q">${(f.plan_words || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    </div>
    ${spots.length ? `<div class="v-sec fe-spots">
      <div class="v-sec-h">Hot spots <span class="tr-n">${spots.length.toLocaleString()}</span></div>
      <ul class="fe-spot-list">${spots.map(spotRow).join("")}</ul>
      <div class="tr-hint">Click a hot spot to see its face alone and point at it.</div>
    </div>` : ""}
    <div class="v-sec tr-parts fe-faces">
      <div class="v-sec-h">Named faces <span class="tr-n">${faces.length.toLocaleString()}</span></div>
      <div class="tr-wrap"><table class="tr-tab">
        <thead><tr><th>Face</th><th>Kind</th><th class="n">Area</th></tr></thead>
        <tbody>${faces.map(faceRow).join("")}</tbody></table></div>
      <div class="tr-hint">Every face is a named surface and node set in the deck. Click one to see it alone; click again for the whole part.</div>
    </div>
    <div class="v-sec fe-dl">
      <div class="v-sec-h">${esc(dl.words || "The download holds:")}</div>
      <ul class="tr-q">${(dl.contains || []).map((c) => `<li><b>${esc(c.file)}</b> - ${esc(c.words)}</li>`).join("")}</ul>
      ${dl.later ? `<div class="tr-hint">${esc(dl.later)}</div>` : ""}
    </div>`;
}

/** The quality summary as a plain-text file beside the mesh download. */
export function feaDownloads(bar, job, f) {
  if (!bar || !f) return null;
  const ck = f.check || {};
  const lines = [f.title || "FEA mesh", f.summary || "", "", `Result: ${OUTCOME[ck.outcome] || ""} - ${ck.words || ""}`,
    `Through-wall check: ${(f.through_wall || {}).words || ""}`, `Units: ${(f.units || {}).words || ""}`, "",
    "Quality, against each limit:"]
    .concat((ck.rows || []).map((r) => `  ${r.ok === false ? "MISS" : r.ok ? "ok  " : "n/a "} ${r.words}`))
    .concat(["", "Per solver:"], (f.solvers || []).flatMap((sv) => [`  ${sv.label} (${sv.file}): ${SOLVER_OUT[sv.outcome] || ""} - ${sv.tested_words}`]
      .concat((sv.rows || []).map((r) => `      ${(r.status || "").toUpperCase().padEnd(10)} ${r.label}: ${fmt(r.measured)}`
        + (r.warn != null ? `; warns ${r.op === "<=" ? "over" : "under"} ${fmt(r.warn)}` : "")
        + (r.error != null ? `; fails ${r.op === "<=" ? "over" : "under"} ${fmt(r.error)}` : "")
        + ` (${r.source})`))))
    .concat(["", "The plan:"], (f.plan_words || []).map((w) => `  - ${w}`))
    .concat(["", "Hot spots:"], (f.hot_spots || []).map((h) => `  ${h.id} ${h.words} (${(h.faces || []).join(", ")})`))
    .concat(["", "Named faces:"], (f.faces || []).map((x) => `  ${x.label}`));
  const text = lines.join("\n") + "\n";
  const a = document.createElement("a"); a.className = "v-dlbtn"; a.href = "#"; a.id = `v-fea-report-${job}`;
  a.innerHTML = `<b>Mesh report</b><span>txt · ${Math.max(1, Math.round(text.length / 1024))} KB</span>`;
  a.title = "The quality summary, the plan and the named faces, as text";
  a.onclick = (e) => {
    e.preventDefault();
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const d = document.createElement("a"); d.href = url; d.download = `fea_mesh_report_${String(job).slice(0, 8)}.txt`;
    document.body.appendChild(d); d.click(); d.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
  };
  bar.appendChild(a);
  return a;
}

/** Wire the section: a face shown alone, a hot spot pointed at. `hooks` = {isolate(face|null),
    point(at_m|null)}. */
export function bindFeaResults(root, job, f, hooks) {
  if (!root || !f) return null;
  // WHAT IS PICKED: a face ("face:Face_3") or a hot spot ("spot:H2") - one key, so picking a hot
  // spot on the face already shown keeps the face and adds the marker, and only the same row
  // clicked again goes back to the whole part
  let picked = null;
  const mark = () => {
    root.querySelectorAll(".fe-face").forEach((tr) => tr.classList.toggle("sel", picked === `face:${tr.dataset.face}`));
    root.querySelectorAll(".fe-spot").forEach((li) => li.classList.toggle("on", picked === `spot:${li.dataset.spot}`));
  };
  const pick = (key, face, at) => {
    picked = picked === key ? null : key;
    if (hooks && hooks.isolate) hooks.isolate(picked ? face : null);
    if (hooks && hooks.point) hooks.point(picked ? at || null : null);
    mark();
  };
  root.querySelectorAll(".fe-face").forEach((tr) => {
    const go = () => pick(`face:${tr.dataset.face}`, tr.dataset.face, null);
    tr.onclick = go;
    tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
  });
  root.querySelectorAll(".fe-spot").forEach((li) => {
    const h = (f.hot_spots || []).find((x) => x.id === li.dataset.spot) || {};
    const go = () => pick(`spot:${li.dataset.spot}`, li.dataset.face, h.at_m || null);
    li.onclick = go;
    li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
  });
  return { pick: (face) => { picked = face ? `face:${face}` : null; mark(); } };
}
