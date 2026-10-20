// Responsibility: Build the FEA part check - the part's size in the file's unit, the unit box, how
// many bodies the file has and what the clean check found, and the one question "Is this your part
// and are the units right?" - and read the answer back out of it.
// Boundaries: markup and a reading of it. No fluid, no openings, no flow axis and no stickers: an
// FEA part is meshed as itself. It fetches nothing and starts no run; the stage and the card both
// use it, and confirming is the callback they were handed.
import { esc } from "../core/format.js";
import { bindUnit, shown, unitBoxHtml, unitChoiceHint, unitChoiceNeeded, unitOf } from "./geometry_form.js";

/** The size row: the three sides in the file's own numbers, the unit beside them. */
function sizeHtml(p) {
  const s = (p.size_mm || []).map((v) => shown(v, p));
  return s.length === 3 ? `${esc(s.join(" × "))} <span class="gc-u">${esc(unitOf(p))}</span>` : "not measured";
}

function bodiesHtml(f) {
  const n = f.bodies;
  if (n === 1) return "1 (a single part)";
  if (n > 1) return `${n} separate bodies`;
  return "not counted";
}

function cleanHtml(f) {
  const mark = f.clean === true ? "✓" : f.clean === false ? "!" : "?";
  const cls = f.clean === true ? "ok" : f.clean === false ? "bad" : "unk";
  return `<span class="fea-clean ${cls}"><span class="ic">${mark}</span>${esc(f.clean_summary || "")}</span>`;
}

/** The panel's body for one check: `f` is the server's `fea` view, `p` the proposal (size, unit). */
export function feaPanelHtml(f, p) {
  const blocked = f.single_part_only || "";
  const why = f.clean === false
    ? `<div class="gc-note">The part can still go ahead: the mesh is made from the file as it is.</div>` : "";
  return `<div class="v-row v-titlerow"><b class="v-title">Your part</b><span class="v-meta">for structural analysis (FEA)</span></div>
    <div class="gc-lead">${p.part ? `<div class="gc-part"><b>${esc(p.part)}</b></div>` : ""}
      <p>The part is meshed as itself, as a solid. Every face is kept as a named group, so you set the supports and loads in your own solver.</p></div>
    <div class="gc-form fea-form">
      <div class="rc-row"><div class="rc-k">Size</div><div class="rc-v fea-size">${sizeHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">The file's unit</div><div class="rc-v">${unitBoxHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">Bodies</div><div class="rc-v fea-bodies">${esc(bodiesHtml(f))}</div></div>
      <div class="rc-row"><div class="rc-k">Clean check</div><div class="rc-v">${cleanHtml(f)}</div></div>
      ${why}
      ${blocked ? `<div class="fea-blocked" role="alert">${esc(blocked)}</div>` : ""}
      <div class="gc-tools"><button class="gc-measure v-btn" type="button" title="measure between two points on the part">Measure</button><span class="gc-tools-hint">click two points to check a length</span></div>
      <p class="fea-q">${esc(f.question || "Is this your part and are the units right?")}</p>
      <div class="gc-actions"><button class="gc-proceed" type="button"${blocked ? " disabled" : ""}>Yes, proceed</button>
        <span class="gc-hint">${blocked ? "Upload a single part to go on." : "Change the unit first if the size is wrong."}</span></div>
    </div>`;
}

/** What the user confirmed: the unit they say the file is in, and the numbers it was read under.
 *  The server sets everything else for an FEA part (a solid body, its bodies and clean check). */
export function readFeaForm(root, p) {
  const body = { part: p.part || "", size_mm: p.size_mm || null, unit: unitOf(p),
                 scale_to_m: p.scale_to_m == null ? null : Number(p.scale_to_m) };
  const upSel = root.closest ? root.closest(".gc-stage")?.querySelector(".v-up select") : null;
  const up = (upSel && upSel.value) || p.up_axis;
  if (up) body.up_axis = up;
  return body;
}

/** Wire the panel: the unit box follows every size shown, and Proceed confirms - after the unit is
 *  picked when the size makes it doubtful. `done()` runs once the server has the answer. */
export function bindFeaPanel(root, f, p, { confirm, done }) {
  bindUnit(root, p);
  const form = root.querySelector(".fea-form") || root;
  const btn = form.querySelector(".gc-proceed"), hint = form.querySelector(".gc-hint");
  if (!btn) return;
  btn.onclick = async () => {
    if (f.single_part_only) return;
    if (unitChoiceNeeded(p)) {
      hint.textContent = unitChoiceHint(p);
      const pick = form.querySelector(".gc-pick"); if (pick) pick.focus();
      return;
    }
    btn.disabled = true; btn.textContent = "Confirming…";
    try {
      await confirm(readFeaForm(form, p), "");
      form.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
      const acts = form.querySelector(".gc-actions");
      if (acts) acts.innerHTML = '<span class="gc-done">✓ Confirmed. Next: what you will analyze.</span>';
      if (done) done();
    } catch (e) {
      btn.disabled = false; btn.textContent = "Yes, proceed";
      hint.textContent = "Could not confirm: " + ((e && e.message) || "try again");
    }
  };
}
