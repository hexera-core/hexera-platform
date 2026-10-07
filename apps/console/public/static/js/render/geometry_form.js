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

/* THE NUMBERS ON THE FORM ARE THE FILE'S OWN. The check reads them under a declared or assumed
   unit (`scale_to_m`, metres per file unit) and serves them as millimetres of that reading; here
   they are shown as the file wrote them, with the unit the user has chosen beside every one, so
   "117" is 117 mm or 117 m as the unit box says - and a blade drawn in metres but read as
   millimetres is corrected where the sizes are, not discovered after the run. The server
   re-reads every length in the unit that stands when the form is confirmed. */
export const UNITS = [["mm", "millimetres"], ["cm", "centimetres"], ["m", "metres"], ["in", "inches"]];
const SCALE = { mm: 0.001, cm: 0.01, m: 1.0, in: 0.0254 };
const raw = (v, p) => v * (0.001 / ((p && p.scale_to_m) || 0.001));
// a number the user types is in the file's own units; the server takes lengths in the reading's
// millimetres, so it goes back through the same scale
const typed = (v, p) => v * (((p && p.scale_to_m) || 0.001) / 0.001);
function fmt3(v) { const a = Math.abs(v); return a >= 100 ? String(Math.round(v)) : a === 0 ? "0" : String(Number(v.toPrecision(3))); }
// a box the user may leave as it is holds the exact value, not a rounded reading of it
const exact = (v) => String(Number(v.toFixed(6)));
export const shown = (v, p) => (v == null || isNaN(v)) ? "?" : fmt3(raw(v, p));
export const unitOf = (p) => (p && SCALE[p.unit] ? p.unit : "mm");
const sym = (p) => `<span class="gc-u">${unitOf(p)}</span>`;
/** A length a person can picture, from metres: 2.66 cm, 1.05 m, 117 m, 1.05 km. */
export function lengthWords(metres) {
  let v, u;
  if (metres >= 1000) { v = metres / 1000; u = "km"; } else if (metres >= 1) { v = metres; u = "m"; }
  else if (metres >= 0.01) { v = metres * 100; u = "cm"; } else { v = metres * 1000; u = "mm"; }
  return `${fmt3(v)} ${u}`;
}
const UNIT_WORDS = Object.fromEntries(UNITS);
/** The other reading the server proposes, while it still stands: the part is implausible in the
 *  unit in effect ("117 mm" for a wind turbine blade) and the user has not picked a unit yet. */
export function unitChoiceNeeded(p) {
  const s = p && p.unit_suggestion;
  return !!(s && SCALE[s.unit] && !p.unit_touched && p.unit_basis !== "user_confirmed" && s.unit !== unitOf(p));
}
/** What the unit row says beside the box: on whose word the unit is, and how long the part then
 *  is - and, when the size makes that unit doubtful, how long it would be in the other one. */
export function unitHint(p) {
  const longest = Math.max(0, ...(p.size_mm || []).map((v) => raw(v, p)));
  const basis = { file_declared: "the file says so", user_confirmed: "confirmed", chosen: "your choice" }[p.unit_basis]
    || "the file does not say - check the size";
  if (longest > 0 && unitChoiceNeeded(p)) {
    const s = p.unit_suggestion;
    return `${basis}, but ${s.why || "the size looks wrong"} · ${readingOf(p, unitOf(p))} long`
      + ` - or ${readingOf(p, s.unit)} if the file is in ${UNIT_WORDS[s.unit]}. Pick one.`;
  }
  return longest > 0 ? `${basis} · the part would be ${lengthWords(longest * SCALE[unitOf(p)])} long` : basis;
}
/** The part's length under one unit: the server's words for the two readings it compared ("117 mm",
 *  "117 m"), else this form's own. */
function readingOf(p, unit) {
  const said = p.unit_suggestion && p.unit_suggestion.sizes && p.unit_suggestion.sizes[unit];
  if (said) return said;
  const longest = Math.max(0, ...(p.size_mm || []).map((v) => raw(v, p)));
  return lengthWords(longest * (SCALE[unit] || 0));
}
/** The two readings as buttons, shown while the choice is open: pressing one sets the box. */
function pickHtml(p) {
  const longest = Math.max(0, ...(p.size_mm || []).map((v) => raw(v, p)));
  const s = p.unit_suggestion || {};
  if (!(longest > 0) || !SCALE[s.unit]) return `<span class="gc-unit-pick" hidden></span>`;
  const btn = (u) => `<button class="gc-pick v-btn" type="button" data-unit="${u}">${esc(readingOf(p, u))} (${esc(UNIT_WORDS[u])})</button>`;
  return `<span class="gc-unit-pick"${unitChoiceNeeded(p) ? "" : " hidden"}>${btn(unitOf(p))}${btn(s.unit)}</span>`;
}
/** What Proceed says when the unit is still open. */
export function unitChoiceHint(p) {
  const s = p.unit_suggestion || {};
  return `Pick the file's unit first: is the part ${readingOf(p, unitOf(p))} or ${readingOf(p, s.unit)} long?`;
}
/* a name lands inside a double-quoted attribute; `esc` covers text, this covers the quote too,
   so a model-given label like 2" inlet neither ends the value early nor smuggles markup in */
export const attr = (v) => esc(v).replace(/"/g, "&quot;");

function sel(cls, opts, cur) {
  return `<select class="${cls}">${opts.map(([v, l]) =>
    `<option value="${v}"${cur === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
}

/** One row of the openings table. An opening the user added off any measured face has no size
 *  yet: its size cell is a box to type the diameter into. */
export function rowHtml(o, p) {
  const id = Number(o.id);
  const size = o.shape === "unknown"
    ? `<input class="gc-num gc-dia" type="number" min="0" step="any" placeholder="${unitOf(p)} across" aria-label="diameter of opening ${id}">`
    : o.shape === "circle" ? `${esc(shown(o.diameter_mm, p))} ${sym(p)} across` : `${esc(shown(o.width_mm, p))} x ${esc(shown(o.height_mm, p))} ${sym(p)}`;
  const at = (o.centroid_mm || []).map((v) => shown(v, p)).join(", ");
  return `<tr data-id="${id}"${o.added ? ' data-added="1"' : ""}><td class="gc-n" title="opening ${id}"><span class="gc-badge">${id}</span></td>
      <td><input class="gc-name" value="${attr(o.name || "")}" maxlength="40" aria-label="name of opening ${id}"></td>
      <td>${sel("gc-role", ROLES, o.role)}</td>
      <td class="gc-dim">${size}</td><td class="gc-dim gc-pos">(${esc(at)}) ${sym(p)}</td>
      <td class="gc-conf" title="how sure the check is">${o.added ? "you" : Math.round((o.confidence || 0) * 100) + "%"}</td>
      <td class="gc-del"><button class="gc-x" type="button" title="remove this opening" aria-label="remove opening ${id}">×</button></td></tr>`;
}

/** The openings table's header row - one copy, for the form and for the first opening added. */
export function tableHead() {
  return `<thead><tr><th>#</th><th>Name</th><th>Role</th><th>Size</th><th class="gc-pos">Position</th><th>Sure</th><th></th></tr></thead>`;
}

/** The form for one proposal: the kind and flow selects, the openings table, the notes, and the
 *  one action. Every row carries its opening id so the reader can find it again. */
export function formHtml(p) {
  const rows = (p.openings || []).map((o) => rowHtml(o, p)).join("");
  const notes = (p.notes || []).map((n) => `<div class="gc-note">${esc(n)}</div>`).join("");
  const ext = p.extents || {};
  // A BODY IN A FLOW: which way the fluid travels, the part's length along it, how far the far
  // field reaches in those lengths, and whether it stands on the ground. Shown only when the
  // flow is around the part; the openings table only when it is through.
  const external = `<div class="gc-ext"${p.flow === "external" ? "" : " hidden"}>
      <div class="rc-row"><div class="rc-k">The fluid travels along</div><div class="rc-v">${sel("gc-sel gc-axis", AXES, p.flow_axis || "unknown")}${p.flow_axis_guessed ? '<span class="gc-guess">a guess - check it</span>' : ""}</div></div>
      <div class="rc-row"><div class="rc-k">Reference length</div><div class="rc-v"><input class="gc-num gc-ref" type="number" min="0" step="any" value="${p.reference_length_mm == null ? 0 : exact(raw(num(p.reference_length_mm, 0), p))}" aria-label="reference length"> ${sym(p)} along the flow</div></div>
      <div class="rc-row"><div class="rc-k">Far field, in lengths</div><div class="rc-v gc-extents">${EXTENTS.map(([k, l]) =>
        `<label>${esc(l)} <input class="gc-num gc-ext-${k}" data-k="${k}" type="number" min="0.5" step="0.5" value="${num(ext[k], 5)}"></label>`).join("")}</div></div>
      <div class="rc-row"><div class="rc-k">On the ground</div><div class="rc-v"><label><input class="gc-ground" type="checkbox"${p.grounded ? " checked" : ""}> the part stands on the ground</label></div></div>
    </div>`;
  return `<div class="rc-row"><div class="rc-k">The file is</div><div class="rc-v">${sel("gc-sel gc-kind", KIND, p.input_kind)}</div></div>
    <div class="rc-row"><div class="rc-k">The file's unit</div><div class="rc-v">${sel("gc-sel gc-unit", UNITS, unitOf(p))}${pickHtml(p)}<span class="gc-unit-hint">${esc(unitHint(p))}</span></div></div>
    <div class="rc-row"><div class="rc-k">The fluid flows</div><div class="rc-v">${sel("gc-sel gc-flow", [["internal", "through the part"], ["external", "around the part"]], p.flow)}</div></div>
    <div class="gc-int"${p.flow === "external" ? " hidden" : ""}>
    ${rows ? `<table class="gc-table">${tableHead()}<tbody>${rows}</tbody></table>`
           : `<div class="gc-note">No openings found. Add one below if the fluid flows through this part.</div>`}
    <div class="gc-tools"><button class="gc-add v-btn" type="button">Add an opening</button><button class="gc-measure v-btn" type="button" title="measure between two points on the part">Measure</button><span class="gc-tools-hint">then click the part where it is</span></div>
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

/** Keep the unit the user chooses in step everywhere a size is shown, and say what the part
 *  then is. Call once after the form is in the document; `p.unit` follows the box. */
function showUnit(root, p) {
  root.querySelectorAll(".gc-u").forEach((el) => { el.textContent = unitOf(p); });
  root.querySelectorAll(".gc-dia").forEach((el) => { el.placeholder = `${unitOf(p)} across`; });
  const hint = root.querySelector(".gc-unit-hint"); if (hint) hint.textContent = unitHint(p);
  const pick = root.querySelector(".gc-unit-pick");
  if (pick) {
    const html = pickHtml(p), box = document.createElement("span"); box.innerHTML = html;
    const fresh = box.firstElementChild;
    if (fresh) { pick.replaceWith(fresh); bindPick(root, p, fresh); }
  }
}
function bindPick(root, p, pick) {
  (pick || root.querySelector(".gc-unit-pick"))?.querySelectorAll(".gc-pick").forEach((b) => {
    b.onclick = () => {
      p.unit = b.dataset.unit; p.unit_basis = "chosen"; p.unit_touched = true;
      const unitSel = root.querySelector(".gc-unit"); if (unitSel) unitSel.value = p.unit;
      showUnit(root, p);
    };
  });
}
export function bindUnit(root, p) {
  const unitSel = root.querySelector(".gc-unit");
  if (!unitSel) return;
  unitSel.addEventListener("change", () => { p.unit = unitSel.value; p.unit_basis = "chosen"; p.unit_touched = true; showUnit(root, p); });
  bindPick(root, p);
  showUnit(root, p);
}

/** A unit settled elsewhere - the chat - reaches a form the user has not set themselves. */
export function followUnit(root, p, unit, basis) {
  if (!unit || !SCALE[unit] || p.unit_touched || unit === p.unit) return;
  p.unit = unit; p.unit_basis = basis || "user_confirmed";
  const unitSel = root.querySelector(".gc-unit"); if (unitSel) unitSel.value = unit;
  showUnit(root, p);
}

/** The other reading, as the server now proposes it (the user's words arrived, the naming
 *  answered, or the unit was settled and there is none) - redrawn on the unit row only. */
export function followSuggestion(root, p, suggestion) {
  const next = suggestion || null, was = p.unit_suggestion || null;
  if (JSON.stringify(next) === JSON.stringify(was)) return;
  p.unit_suggestion = next;
  showUnit(root, p);
}

/** The part's length along a flow axis ("+x", "-y", ...), in the reading's millimetres, from the
 *  proposal's size; null for "not sure" or a part with no size. */
export function extentAlong(p, axis) {
  const k = { x: 0, y: 1, z: 2 }[String(axis || "").slice(-1)];
  const s = p && p.size_mm;
  if (k === undefined || !/^[+-][xyz]$/.test(String(axis)) || !s || s.length !== 3) return null;
  const v = Number(s[k]);
  return v > 0 ? v : null;
}

/* THE REFERENCE LENGTH FOLLOWS THE FLOW AXIS. The check fills it with the part's length along the
   axis it guessed; when that guess is wrong and the user turns the axis, the old length must not
   stay behind - the far field is sized in reference lengths, so the NASA CRM read along its span
   got a box half the size, with nothing on the form to say so. It follows the axis until the user
   types a length of their own, which is theirs from then on (a blank box follows again). A turn
   along the same line (+x to -x) is the same length and leaves the box as it stands. */

/** Turn the form to a flow axis as the select does: the axis is the user's, and an untyped
 *  reference length becomes the part's length along it. */
export function followAxis(root, p, axis) {
  const axisEl = root.querySelector(".gc-axis"), refEl = root.querySelector(".gc-ref");
  if (axisEl && axisEl.value !== axis) axisEl.value = axis;
  const was = String(p.flow_axis || "");
  p.flow_axis = axis; p.flow_axis_guessed = false; p.flow_axis_touched = true;
  if (p.reference_length_typed || was.slice(-1) === String(axis).slice(-1)) return;
  const along = extentAlong(p, axis);
  if (along === null) return;                  // "not sure" keeps the length that stands
  p.reference_length_mm = along;
  if (refEl) refEl.value = exact(raw(along, p));
}

/** Keep the reference length in step with the axis select, and mark it the user's once they
 *  type one. Call once after the form is in the document. */
export function bindAxis(root, p) {
  const axisEl = root.querySelector(".gc-axis"), refEl = root.querySelector(".gc-ref");
  if (axisEl) axisEl.addEventListener("change", () => followAxis(root, p, axisEl.value));
  if (!refEl) return;
  refEl.addEventListener("input", () => {
    const v = num(refEl.value, 0);
    p.reference_length_typed = v > 0;
    if (v > 0) p.reference_length_mm = typed(v, p);
  });
  // a box left blank is the part's length along the flow again, shown as soon as the user leaves it
  refEl.addEventListener("change", () => {
    if (num(refEl.value, 0) > 0) return;
    const along = extentAlong(p, axisEl ? axisEl.value : p.flow_axis);
    if (along === null) return;
    p.reference_length_mm = along;
    refEl.value = exact(raw(along, p));
  });
}

/** The external-flow answers as the form holds them now; the reference length typed in the
 *  file's units, read back in the reading's millimetres. */
export function readExternal(root, p) {
  const ext = {};
  // a margin below half a body length is no far field at all; a blank box keeps the default
  root.querySelectorAll(".gc-extents input[data-k]").forEach((el) => { ext[el.dataset.k] = Math.max(0.5, num(el.value, 5)); });
  const axisEl = root.querySelector(".gc-axis"), refEl = root.querySelector(".gc-ref"), gEl = root.querySelector(".gc-ground");
  const axis = axisEl ? axisEl.value : "unknown", ref = refEl ? num(refEl.value, 0) : 0;
  return { flow_axis: axis,
           // a blank box is the part's length along the flow, never no length at all
           reference_length_mm: ref > 0 ? typed(ref, p) : extentAlong(p, axis),
           // the server corrects a length left behind by a turned axis, never one the user typed
           reference_length_typed: !!(p && p.reference_length_typed),
           extents: ext, grounded: !!(gEl && gEl.checked) };
}

export function readForm(root, p) {
  const flow = root.querySelector(".gc-flow").value;
  const openings = flow === "external" ? [] : [...root.querySelectorAll("tbody tr")].map((tr) => {
    const id = Number(tr.dataset.id), o = (p.openings || []).find((x) => x.id === id) || {};
    const body = { id, name: tr.querySelector(".gc-name").value.trim() || o.name || `opening_${id}`,
                   role: tr.querySelector(".gc-role").value, centroid_mm: o.centroid_mm || null };
    // which way the mouth faces, out of the part - measured, never typed - when it is known
    if (Array.isArray(o.normal) && o.normal.length === 3 && o.normal.every((v) => Number.isFinite(Number(v)))) {
      body.normal = o.normal.map(Number);
    }
    const dia = tr.querySelector(".gc-dia");
    if (dia) { const v = num(dia.value, 0); if (v > 0) body.diameter_mm = typed(v, p); }    // typed in the file's units
    else if (o.shape === "circle") body.diameter_mm = o.diameter_mm;
    else { body.width_mm = o.width_mm; body.height_mm = o.height_mm; body.diameter_mm = o.diameter_mm; }
    return body;
  });
  const body = { input_kind: root.querySelector(".gc-kind").value, flow, part: p.part || "",
                 openings, seed_point_mm: p.seed_point_mm || null, size_mm: p.size_mm || null,
                 // the scale the numbers were read under and the unit the user says the file is
                 // in, for the server to record the unit and re-read every length in it
                 scale_to_m: p.scale_to_m == null ? null : Number(p.scale_to_m), unit: unitOf(p) };
  if (flow === "external") Object.assign(body, readExternal(root, p));
  // WHICH WAY IS UP: the stage's Up control where it has one, else what the check proposed
  const upSel = root.closest ? root.closest(".gc-stage")?.querySelector(".v-up select") : null;
  const up = (upSel && upSel.value) || p.up_axis;
  if (up) body.up_axis = up;
  return body;
}

/* WHAT THE FORM WAS DRAWN WITH. The model's names land while the user may already be editing, and
   the form is drawn again from the proposal: a select or a box the user changed went back to the
   check's value, and Proceed then confirmed the check's far field, not theirs. `noteDrawn` keeps
   what a fresh form shows; `changedAnswers` says which answers the user has changed since, as
   proposal fields a re-draw can keep. Names, roles, the unit, the flow axis and a typed reference
   length are kept by their own rules where the form is re-drawn. */
function formAnswers(root) {
  const v = (s) => { const el = root.querySelector(s); return el ? el.value : undefined; };
  const extents = {};
  root.querySelectorAll(".gc-extents input[data-k]").forEach((el) => { extents[el.dataset.k] = el.value; });
  const g = root.querySelector(".gc-ground");
  return { input_kind: v(".gc-kind"), flow: v(".gc-flow"), extents, grounded: g ? g.checked : undefined };
}
export function noteDrawn(root) { if (root) root._drawn = formAnswers(root); }
export function changedAnswers(root) {
  const was = root && root._drawn;
  if (!was) return {};
  const now = formAnswers(root), out = {};
  if (now.input_kind !== was.input_kind) out.input_kind = now.input_kind;
  if (now.flow !== was.flow) out.flow = now.flow;
  if (Object.keys(now.extents).some((k) => now.extents[k] !== was.extents[k])) {
    out.extents = Object.fromEntries(Object.entries(now.extents).map(([k, x]) => [k, Math.max(0.5, num(x, 5))]));
  }
  if (now.grounded !== was.grounded) out.grounded = now.grounded;
  return out;
}

/** The check's proposal as the server now serves it, laid over what it served before: what the
 *  user's answers are compared with. Openings join by id, so the naming's words meet the scout's
 *  measurements. Never holds anything the user typed. */
export function servedProposal(before, next) {
  const a = before || {}, b = next ? JSON.parse(JSON.stringify(next)) : {};
  const byId = new Map((a.openings || []).map((o) => [Number(o.id), o]));
  (b.openings || []).forEach((o) => byId.set(Number(o.id), { ...(byId.get(Number(o.id)) || {}), ...o }));
  return { ...a, ...b, openings: [...byId.values()] };
}

/* WHAT THE USER CHANGED, SAID BACK. Every answer on the form travels with Proceed and the server
   records it, but the chat only showed the whole declaration afterwards, so an edit read like the
   check's own proposal and nothing said "got it". This is the one line that does: each change
   against what the check proposed, in plain words. "" when nothing was changed. */
const KIND_WORDS = { "body-surface": "a hollow wall", "fluid-domain": "the fluid volume", "solid-body": "a solid body" };
const FLOW_WORDS = { internal: "through the part", external: "around the part" };
const ROLE_WORDS = { inlet: "an inlet", outlet: "an outlet", not_an_opening: "not an opening" };
const MAX_EDITS = 6;
export function editsLine(served, body, p) {
  const s = served || {}, out = [];
  const kindWas = s.input_kind || KIND[0][0], flowWas = s.flow || "internal";
  if (body.input_kind && body.input_kind !== kindWas) {
    out.push(`the file is ${KIND_WORDS[body.input_kind] || body.input_kind} (was ${KIND_WORDS[kindWas] || kindWas})`);
  }
  if (body.flow && body.flow !== flowWas) out.push(`the fluid flows ${FLOW_WORDS[body.flow]} (was ${FLOW_WORDS[flowWas]})`);
  if (body.unit && body.unit !== unitOf(s)) out.push(`the file is in ${UNIT_WORDS[body.unit]} (was ${UNIT_WORDS[unitOf(s)]})`);
  if (body.flow === "external") {
    const axisWas = s.flow_axis || "unknown";
    if (body.flow_axis && body.flow_axis !== axisWas) {
      const said = (a) => (a === "unknown" ? "not sure" : a);
      out.push(`the fluid travels along ${said(body.flow_axis)}` + (axisWas === "unknown" ? "" : ` (was ${axisWas})`));
    }
    if (body.reference_length_typed && body.reference_length_mm > 0) {
      out.push(`reference length ${shown(body.reference_length_mm, p)} ${unitOf(p)}`);
    }
    const far = EXTENTS.filter(([k]) => body.extents && num(body.extents[k], 5) !== num((s.extents || {})[k], 5))
      .map(([k, l]) => `${num(body.extents[k], 5)} ${l} (was ${num((s.extents || {})[k], 5)})`);
    if (far.length) out.push(`far field, in part lengths: ${far.join(", ")}`);
    if (!!body.grounded !== !!s.grounded) out.push(body.grounded ? "the part stands on the ground" : "the part is not on the ground");
  } else {
    const was = new Map((s.openings || []).map((o) => [Number(o.id), o]));
    const kept = new Set();
    (body.openings || []).forEach((o) => {
      const id = Number(o.id), w = was.get(id);
      kept.add(id);
      if (!w) { out.push(`opening ${id} added as ${o.name} (${ROLE_WORDS[o.role] || o.role})`); return; }
      if (o.name !== (w.name || `opening_${id}`)) out.push(`opening ${id} named ${o.name}`);
      if (o.role !== w.role) out.push(`opening ${id} is ${ROLE_WORDS[o.role] || o.role} (was ${ROLE_WORDS[w.role] || w.role || "unset"})`);
    });
    was.forEach((_w, id) => { if (!kept.has(id)) out.push(`opening ${id} removed`); });
  }
  if (body.up_axis && body.up_axis !== (s.up_axis || "+z")) out.push(`up is ${body.up_axis} (was ${s.up_axis || "+z"})`);
  if (!out.length) return "";
  const listed = out.slice(0, MAX_EDITS), more = out.length - listed.length;
  return "Noted your changes on the picture: " + listed.join("; ") + (more ? `; and ${more} more` : "") + ".";
}

/** Lock a form after it was accepted, and say so where the action was. */
export function markConfirmed(root) {
  root.querySelectorAll("input,select").forEach((el) => { el.disabled = true; });
  const acts = root.querySelector(".gc-actions");
  if (acts) acts.innerHTML = '<span class="gc-done">✓ Confirmed. The questions that follow will skip all of this.</span>';
}
