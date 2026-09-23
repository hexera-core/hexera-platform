// Responsibility: Build the geometry-check form - what the file is, which way the fluid goes, and
// one editable row per numbered opening - and read the user's answer back out of it.
// Boundaries: markup and a reading of it. It fetches nothing, draws no scene, and decides nothing
// about what is right; the card and the 3D stage both use it so the user answers one form.
import { esc } from "../core/format.js";

/* the two prefixes the conversation carries for the intake's sake; people see the words after them */
export const DRAWING_MARK = "GEOMETRY CHECK (drawing your part):";
export const CONFIRMED_MARK = "GEOMETRY CHECK (confirmed by the user):";

/** A stored message as a person should read it: the marks the intake reads are taken off. */
export function displayText(text) {
  const t = String(text == null ? "" : text);
  if (t.startsWith(DRAWING_MARK)) { const r = t.slice(DRAWING_MARK.length).trim(); return r.charAt(0).toUpperCase() + r.slice(1); }
  if (t.startsWith(CONFIRMED_MARK)) return "Confirmed on the picture: " + t.slice(CONFIRMED_MARK.length).trim();
  return t;
}

export const KIND = [["body-surface", "the part's wall, hollow inside for the fluid"],
                     ["fluid-domain", "the fluid volume itself"],
                     ["solid-body", "a solid body the fluid flows around"]];
export const ROLES = [["inlet", "inlet"], ["outlet", "outlet"], ["not_an_opening", "not an opening"]];
export const AXES = [["+x", "+x"], ["-x", "-x"], ["+y", "+y"], ["-y", "-y"], ["+z", "+z"], ["-z", "-z"],
                     ["unknown", "not sure"]];
const EXTENTS = [["upstream", "upstream"], ["downstream", "downstream"], ["lateral", "to each side"],
                 ["vertical", "above"]];
// a blank box is a box the user has not answered, never a zero
const num = (v, d) => (v == null || v === "" || isNaN(v)) ? d : Number(v);

export const mm = (v) => (v == null || isNaN(v)) ? "?" : String(Math.round(v));
/* a name lands inside a double-quoted attribute; `esc` covers text, this covers the quote too,
   so a model-given label like 2" inlet neither ends the value early nor smuggles markup in */
export const attr = (v) => esc(v).replace(/"/g, "&quot;");

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
    return `<tr data-id="${Number(o.id)}"><td class="gc-n" title="opening ${Number(o.id)}">${Number(o.id)}</td>
      <td><input class="gc-name" value="${attr(o.name || "")}" maxlength="40" aria-label="name of opening ${Number(o.id)}"></td>
      <td>${sel("gc-role", ROLES, o.role)}</td>
      <td class="gc-dim">${esc(size)}</td><td class="gc-dim gc-pos">(${esc(at)}) mm</td>
      <td class="gc-conf" title="how sure the check is">${Math.round((o.confidence || 0) * 100)}%</td></tr>`;
  }).join("");
  const notes = (p.notes || []).map((n) => `<div class="gc-note">${esc(n)}</div>`).join("");
  const ext = p.extents || {};
  // A BODY IN A FLOW: which way the fluid travels, the part's length along it, how far the far
  // field reaches in those lengths, and whether it stands on the ground. Shown only when the
  // flow is around the part; the openings table only when it is through.
  const external = `<div class="gc-ext"${p.flow === "external" ? "" : " hidden"}>
      <div class="rc-row"><div class="rc-k">The fluid travels along</div><div class="rc-v">${sel("gc-sel gc-axis", AXES, p.flow_axis || "unknown")}${p.flow_axis_guessed ? '<span class="gc-guess">a guess - check it</span>' : ""}</div></div>
      <div class="rc-row"><div class="rc-k">Reference length</div><div class="rc-v"><input class="gc-num gc-ref" type="number" min="1" step="1" value="${num(p.reference_length_mm, 0)}" aria-label="reference length in millimetres"> mm along the flow</div></div>
      <div class="rc-row"><div class="rc-k">Far field, in lengths</div><div class="rc-v gc-extents">${EXTENTS.map(([k, l]) =>
        `<label>${esc(l)} <input class="gc-num gc-ext-${k}" data-k="${k}" type="number" min="0.5" step="0.5" value="${num(ext[k], 5)}"></label>`).join("")}</div></div>
      <div class="rc-row"><div class="rc-k">On the ground</div><div class="rc-v"><label><input class="gc-ground" type="checkbox"${p.grounded ? " checked" : ""}> the part stands on the ground</label></div></div>
    </div>`;
  return `<div class="rc-row"><div class="rc-k">The file is</div><div class="rc-v">${sel("gc-sel gc-kind", KIND, p.input_kind)}</div></div>
    <div class="rc-row"><div class="rc-k">The fluid flows</div><div class="rc-v">${sel("gc-sel gc-flow", [["internal", "through the part"], ["external", "around the part"]], p.flow)}</div></div>
    <div class="gc-int"${p.flow === "external" ? " hidden" : ""}>
    ${rows ? `<table class="gc-table"><thead><tr><th>#</th><th>name</th><th>role</th><th>size</th><th class="gc-pos">position</th><th>sure</th></tr></thead><tbody>${rows}</tbody></table>`
           : `<div class="gc-note">No openings found. If the fluid flows through this part, say so below and the intake will ask for them.</div>`}
    </div>
    ${external}
    ${notes}
    <div class="gc-actions"><button class="gc-proceed" type="button">Looks right, proceed</button>
      <span class="gc-hint">Fix any name or role first. The questions that follow skip everything confirmed here.</span></div>`;
}

/** What the user confirmed, read from a rendered form: the body the confirm endpoint takes. A
 *  flow around the part has no openings whatever the table says. */
/** Show the part of the form that applies to the chosen flow, and keep it so as the user
 *  changes the select. Call once after the form is in the document. */
export function applyFlow(root) {
  const flowSel = root.querySelector(".gc-flow");
  if (!flowSel) return;
  const show = () => {
    const ext = flowSel.value === "external";
    root.querySelectorAll(".gc-int").forEach((el) => { el.hidden = ext; });
    root.querySelectorAll(".gc-ext").forEach((el) => { el.hidden = !ext; });
  };
  flowSel.addEventListener("change", show);
  show();
}

/** The external-flow answers as the form holds them now. */
export function readExternal(root) {
  const ext = {};
  // a margin below half a body length is no far field at all; a blank box keeps the default
  root.querySelectorAll(".gc-extents input[data-k]").forEach((el) => { ext[el.dataset.k] = Math.max(0.5, num(el.value, 5)); });
  const axisEl = root.querySelector(".gc-axis"), refEl = root.querySelector(".gc-ref"), gEl = root.querySelector(".gc-ground");
  return { flow_axis: axisEl ? axisEl.value : "unknown",
           reference_length_mm: refEl && num(refEl.value, 0) > 0 ? num(refEl.value, 0) : null,
           extents: ext, grounded: !!(gEl && gEl.checked) };
}

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
  const body = { input_kind: root.querySelector(".gc-kind").value, flow, part: p.part || "",
                 openings, seed_point_mm: p.seed_point_mm || null, size_mm: p.size_mm || null };
  if (flow === "external") Object.assign(body, readExternal(root));
  return body;
}

/** Lock a form after it was accepted, and say so where the action was. */
export function markConfirmed(root) {
  root.querySelectorAll("input,select").forEach((el) => { el.disabled = true; });
  const acts = root.querySelector(".gc-actions");
  if (acts) acts.innerHTML = '<span class="gc-done">✓ Confirmed. The questions that follow will skip all of this.</span>';
}
