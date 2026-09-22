// Responsibility: Build the geometry-check form - what the file is, which way the fluid goes, and
// one editable row per numbered opening - and read the user's answer back out of it.
// Boundaries: markup and a reading of it. It fetches nothing, draws no scene, and decides nothing
// about what is right; the card and the 3D stage both use it so the user answers one form.
import { esc } from "../core/format.js";

export const KIND = [["body-surface", "the part's wall, hollow inside for the fluid"],
                     ["fluid-domain", "the fluid volume itself"],
                     ["solid-body", "a solid body the fluid flows around"]];

export const mm = (v) => (v == null || isNaN(v)) ? "?" : String(Math.round(v));

function sel(cls, opts, cur) {
  return `<select class="${cls}">${opts.map(([v, l]) =>
    `<option value="${v}"${cur === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
}

/** The form for one proposal: the kind and flow selects, the openings table, the notes, and the
 *  one action. Every row carries its opening id so the reader can find it again. */
export function formHtml(p) {
  const rows = (p.openings || []).map((o) => {
    const size = o.shape === "circle" ? `${mm(o.diameter_mm)} mm across`
                                      : `${mm(o.width_mm)} x ${mm(o.height_mm)} mm`;
    const at = (o.centroid_mm || []).map((v) => mm(v)).join(", ");
    return `<tr data-id="${o.id}"><td class="gc-n" title="opening ${o.id}">${o.id}</td>
      <td><input class="gc-name" value="${esc(o.name || "")}" maxlength="40" aria-label="name of opening ${o.id}"></td>
      <td>${sel("gc-role", [["inlet", "inlet"], ["outlet", "outlet"]], o.role)}</td>
      <td class="gc-dim">${esc(size)}</td><td class="gc-dim gc-pos">(${esc(at)}) mm</td>
      <td class="gc-conf" title="how sure the check is">${Math.round((o.confidence || 0) * 100)}%</td></tr>`;
  }).join("");
  const notes = (p.notes || []).map((n) => `<div class="gc-note">${esc(n)}</div>`).join("");
  return `<div class="rc-row"><div class="rc-k">The file is</div><div class="rc-v">${sel("gc-sel gc-kind", KIND, p.input_kind)}</div></div>
    <div class="rc-row"><div class="rc-k">The fluid flows</div><div class="rc-v">${sel("gc-sel gc-flow", [["internal", "through the part"], ["external", "around the part"]], p.flow)}</div></div>
    ${rows ? `<table class="gc-table"><thead><tr><th>#</th><th>name</th><th>role</th><th>size</th><th class="gc-pos">position</th><th>sure</th></tr></thead><tbody>${rows}</tbody></table>`
           : `<div class="gc-note">No openings found: the fluid flows around the whole body.</div>`}
    ${notes}
    <div class="gc-actions"><button class="gc-proceed" type="button">Looks right, proceed</button>
      <span class="gc-hint">Fix any name or role first. The questions that follow skip everything confirmed here.</span></div>`;
}

/** What the user confirmed, read from a rendered form: the body the confirm endpoint takes. A
 *  flow around the part has no openings whatever the table says. */
export function readForm(root, p) {
  const flow = root.querySelector(".gc-flow").value;
  const openings = flow === "external" ? [] : [...root.querySelectorAll("tbody tr")].map((tr) => {
    const id = Number(tr.dataset.id), o = (p.openings || []).find((x) => x.id === id) || {};
    const body = { id, name: tr.querySelector(".gc-name").value.trim() || o.name || `opening_${id}`,
                   role: tr.querySelector(".gc-role").value, centroid_mm: o.centroid_mm || null };
    if (o.shape === "circle") body.diameter_mm = o.diameter_mm;
    else { body.width_mm = o.width_mm; body.height_mm = o.height_mm; body.diameter_mm = o.diameter_mm; }
    return body;
  });
  return { input_kind: root.querySelector(".gc-kind").value, flow, part: p.part || "",
           openings, seed_point_mm: p.seed_point_mm || null, size_mm: p.size_mm || null };
}

/** Lock a form after it was accepted, and say so where the action was. */
export function markConfirmed(root) {
  root.querySelectorAll("input,select").forEach((el) => { el.disabled = true; });
  const acts = root.querySelector(".gc-actions");
  if (acts) acts.innerHTML = '<span class="gc-done">✓ Confirmed. The questions that follow will skip all of this.</span>';
}
